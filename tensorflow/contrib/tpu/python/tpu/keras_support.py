# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""*Experimental* support for running Keras models on the TPU.

To use, wrap your model with the `keras_support.tpu_model` function.

Example usage:

```
image = tf.keras.layers.Input(shape=(28, 28, 3), name='image')
c1 = tf.keras.layers.Conv2D(filters=16, kernel_size=(3, 3))( image)
flattened = tf.keras.layers.Flatten()(c1)
logits = tf.keras.layers.Dense(10, activation='softmax')(flattened)
model = tf.keras.Model(inputs=[image], outputs=[logits])

strategy = keras_support.TPUDistributionStrategy(num_cores_per_host=8)
model = keras_support.tpu_model(model,
                                strategy=strategy,
                                tpu_name_or_address=tpu_name)

# Only TF optimizers are currently supported.
model.compile(optimizer=tf.train.AdamOptimizer(), ...)

# `images` and `labels` should be Numpy arrays.  Support for tensor input
# (e.g. datasets) is planned.
model.fit(images, labels)
```
"""

# pylint: disable=protected-access

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import collections
import contextlib
import re
import sys
import time

import numpy as np

from tensorflow.contrib.cluster_resolver.python.training import tpu_cluster_resolver as tpu_cluster_resolver_lib
from tensorflow.contrib.framework.python.framework import experimental
from tensorflow.contrib.tpu.proto import compilation_result_pb2 as tpu_compilation_result
from tensorflow.contrib.tpu.python.ops import tpu_ops
from tensorflow.contrib.tpu.python.tpu import keras_tpu_variables
from tensorflow.contrib.tpu.python.tpu import tpu
from tensorflow.contrib.tpu.python.tpu import tpu_function
from tensorflow.contrib.tpu.python.tpu import tpu_optimizer
from tensorflow.contrib.tpu.python.tpu import tpu_system_metadata as tpu_system_metadata_lib
from tensorflow.core.protobuf import config_pb2
from tensorflow.python.client import session as tf_session
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.data.ops import iterator_ops
from tensorflow.python.eager import context
from tensorflow.python.estimator import model_fn as model_fn_lib
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_spec
from tensorflow.python.keras import backend as K
from tensorflow.python.keras import callbacks as cbks
from tensorflow.python.keras import models
from tensorflow.python.keras import optimizers as keras_optimizers
from tensorflow.python.keras.engine import base_layer
from tensorflow.python.keras.engine import training_arrays
from tensorflow.python.keras.engine import training_utils
from tensorflow.python.keras.layers import embeddings
from tensorflow.python.keras.utils.generic_utils import make_batches
from tensorflow.python.keras.utils.generic_utils import slice_arrays
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import gen_linalg_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.platform import tf_logging as logging


_SESSIONS = {}


def tpu_session(cluster_resolver):
  """Construct or return a `tf.Session` connected to the given cluster."""
  global _SESSIONS
  master = cluster_resolver.master()
  if master not in _SESSIONS:
    cluster_spec = cluster_resolver.cluster_spec()
    config = config_pb2.ConfigProto(isolate_session_state=True)
    if cluster_spec:
      config.cluster_def.CopyFrom(cluster_spec.as_cluster_def())

    logging.info('Connecting to: %s', master)
    graph = ops.Graph()
    session = tf_session.Session(graph=graph, target=master, config=config)
    with graph.as_default():
      session.run(tpu.initialize_system())

    _SESSIONS[master] = session
  return _SESSIONS[master]


def reset_tpu_sessions():
  _SESSIONS.clear()

try:
  from scipy.sparse import issparse  # pylint: disable=g-import-not-at-top
except ImportError:
  issparse = None


def get_tpu_system_metadata(tpu_cluster_resolver):
  """Retrieves TPU system metadata given a TPUClusterResolver."""
  master = tpu_cluster_resolver.master()

  # pylint: disable=protected-access
  cluster_spec = tpu_cluster_resolver.cluster_spec()
  cluster_def = cluster_spec.as_cluster_def() if cluster_spec else None
  tpu_system_metadata = (
      tpu_system_metadata_lib._query_tpu_system_metadata(
          master,
          cluster_def=cluster_def,
          query_topology=False))

  return tpu_system_metadata


class TPUDistributionStrategy(object):
  """The strategy to run Keras model on TPU."""

  def __init__(self, tpu_cluster_resolver=None, using_single_core=False):
    """Construct a TPUDistributionStrategy.

    Args:
      tpu_cluster_resolver: Any instance of `TPUClusterResolver`. If None, will
        create one with '' as master address.
      using_single_core: Bool. This is the debugging option, which might be
        removed in future once the model replication functionality is mature
        enough. If `False` (default behavior), the system automatically finds
        the best configuration, in terms of number of TPU cores, for the model
        replication, typically using all avaiable TPU cores. If overwrites as
        `True`, force the model replication using single core, i.e., no
        replication.
    """

    if tpu_cluster_resolver is None:
      tpu_cluster_resolver = tpu_cluster_resolver_lib.TPUClusterResolver('')

    metadata = get_tpu_system_metadata(tpu_cluster_resolver)
    self._tpu_metadata = metadata
    self._tpu_cluster_resolver = tpu_cluster_resolver
    self._num_cores = 1 if using_single_core else metadata.num_cores

    # Walk device list to identify TPU worker for enqueue/dequeue operations.
    worker_re = re.compile('/job:([^/]+)')
    for device in metadata.devices:
      if 'TPU:0' in device.name:
        self._worker_name = worker_re.search(device.name).group(1)
        break

  def _make_assignment_for_model(self, cpu_model):
    """Makes a `TPUAssignment` for the passed in `cpu_model`."""
    num_cores = self._num_cores
    if num_cores > 1 and cpu_model.stateful:
      logging.warning(
          'Model replication does not currently support stateful models.  '
          'Degrading to a single core.')
      num_cores = 1

    return TPUAssignment(
        worker_name=self._worker_name, num_cores=num_cores)


class TPUAssignment(object):
  """This is object holding TPU resources assignment for the concrete model.

  `TPUDistributionStrategy` is responsible to create the instance of
  `TPUAssignment`, so, it can dynamically adjust the `num_cores` to use based on
  model and input batch sizes.
  """

  def __init__(self, worker_name, num_cores):
    self._worker_name = worker_name
    self._num_cores = num_cores

  @property
  def worker_name(self):
    return self._worker_name

  @property
  def num_towers(self):
    # TODO(xiejw): Support automatically assign num_cores based on inputs.
    return self._num_cores


class TPUEmbedding(embeddings.Embedding):
  """TPU compatible embedding layer.

  The default Keras layer is not TPU compatible.  This layer is a drop-in
  replacement: it has the same behavior and will work on CPU and GPU devices.
  """

  def build(self, input_shape):
    if input_shape[0] is None:
      raise ValueError(
          'TPUEmbeddings must have a fixed input_length or input shape.')
    return super(TPUEmbedding, self).build(input_shape)

  def call(self, inputs):
    if K.dtype(inputs) != 'int32':
      inputs = math_ops.cast(inputs, 'int32')

    inputs = array_ops.one_hot(inputs, self.input_dim)
    return math_ops.tensordot(inputs, self.embeddings, 1)


class KerasCrossShardOptimizer(keras_optimizers.Optimizer):
  """An optimizer that averages gradients across TPU shards."""

  def __init__(self, opt, name='KerasCrossShardOptimizer'):
    """Construct a new cross-shard optimizer.

    Args:
      opt: An existing `Optimizer` to encapsulate.
      name: Optional name prefix for the operations created when applying
        gradients. Defaults to "KerasCrossShardOptimizer".

    Raises:
      ValueError: If reduction is not a valid cross-shard reduction.
    """
    super(KerasCrossShardOptimizer, self).__init__()
    self._name = name
    self._opt = opt

  def get_updates(self, loss, params):
    logging.info('Get updates: %s', loss)
    self._opt.get_gradients = self.get_gradients
    return self._opt.get_updates(loss, params)

  def get_gradients(self, loss, params):
    num_shards = tpu_function.get_tpu_context().number_of_shards
    grads = super(KerasCrossShardOptimizer, self).get_gradients(loss, params)
    return [tpu_ops.cross_replica_sum(grad) / num_shards for grad in grads]

  def set_weights(self, weights):
    # TODO(power): Figure out whether we really need this given there is no
    # caller for this API yet.
    self._opt.set_weights()

  def get_weights(self):
    return self._opt.get_weights()

  @property
  def lr(self):
    return self._opt.lr


class TPUModelOp(
    collections.namedtuple('TPUModelOp', [
        'compile_op', 'execute_op', 'infeed_tensors', 'infeed_op', 'outfeed_op'
    ])):
  pass


def _valid_name(tensor_name):
  """Return a valid tensor name (strips '/', ':', etc)."""
  return re.sub('[^a-zA-Z0-9_-]+', '', tensor_name)


def _replicated_optimizer(opt):
  """Wrap the optimizer `opt` with CrossShardOptimizer if applicable."""
  # Always wrap `opt` with CrossShardOptimizer, even if we are running on a
  # single core.  This ensures Keras properly tracks and initializes optimizer
  # variables.
  if isinstance(opt, keras_optimizers.TFOptimizer):
    return tpu_optimizer.CrossShardOptimizer(opt.optimizer)
  else:
    return KerasCrossShardOptimizer(opt)


class TPURewriteContext(object):
  """Prepare the environment for a Keras model during `tpu.rewrite`.

  This overrides the default placeholder behaviour to instead refer to a preset
  input mapping.  Placeholders are unsupported in TPU compiled code, and must
  be replaced with explicit inputs or values from the infeed queue.

  Instead of explicitly threading inputs all the way through the Keras codebase,
  we override the behavior of the placeholder while compiling and inject the
  Tensors from the infeed in place of the placeholder.

  Similarly, as we compile a new sub-graph for each unique shape and execution
  mode, we need to override the behavior of an embedded `name_scope` call in
  the base Keras layer code.  This allows us to re-use the same weights across
  many compiles and share a single session/graph.
  """

  def __init__(self, input_map):
    self._input_map = input_map
    self._default_placeholder = None
    self._default_name_scope = None

  def __enter__(self):

    def _placeholder(dtype, shape=None, name=None):  # pylint: disable=unused-argument
      logging.info('Remapping placeholder for %s', name)
      if name in self._input_map:
        return self._input_map[name]
      else:
        logging.info('Default: %s', name)
        return self._default_placeholder(dtype, shape, name)

    def _name_scope(name, default_name=None, values=None):
      caller_frame = sys._getframe().f_back
      caller_obj = caller_frame.f_locals.get('self')
      if (caller_obj is not None and
          isinstance(caller_obj, base_layer.Layer) and name is not None):
        return variable_scope.variable_scope(
            name, default_name, values, reuse=variable_scope.AUTO_REUSE)

      return self._default_name_scope(name, default_name, values)

    self._default_placeholder = array_ops.placeholder
    self._default_name_scope = ops.name_scope
    self._default_make_variable = base_layer.make_variable
    self._default_random_normal = random_ops.random_normal
    self._default_qr = gen_linalg_ops.qr

    array_ops.placeholder = _placeholder

    # Replace random_ops.random_normal with a dummy function because
    # `random_normal` isn't yet implemented on the TPU. Because these
    # initialized values are overwritten by the CPU values, this is okay.
    def random_normal(shape,
                      mean=0.0,
                      stddev=1.0,
                      dtype=dtypes.float32,
                      seed=None,
                      name=None):
      del mean
      del stddev
      del seed
      return array_ops.zeros(shape, dtype=dtype, name=name)

    random_ops.random_normal = random_normal

    # Replace gen_linalg_ops.qr because QR decomposition is not yet implemented.
    # TODO(saeta): Remove qr override once we confirm the qr implementation is
    # ok.
    # pylint: disable=redefined-builtin
    def qr(input, full_matrices=False, name=None):
      """Dummy implementation of qr decomposition."""
      del full_matrices  # TODO(saeta): Properly handle the full matrix case.
      input_shape = input.shape
      if len(input_shape) < 2:
        raise ValueError('Invalid shape passed to qr: %s' % input_shape)
      p = min(input_shape[-1], input_shape[-2])
      if len(input_shape) == 2:
        q = array_ops.zeros((p, p), name=name)
        r = array_ops.zeros(input_shape, name=name)
        return (r, q)
      elif len(input_shape) == 3:
        n = input_shape[0]
        q = array_ops.zeros((n, p, p), name=name)
        r = array_ops.zeros(input_shape, name=name)
        return (r, q)
      else:
        raise ValueError('Invalid shape passed to qr: %s' % input_shape)
    gen_linalg_ops.qr = qr

    ops.name_scope = _name_scope
    base_layer.make_variable = variable_scope.get_variable
    logging.info('Overriding default placeholder.')
    return

  def __exit__(self, exc_type, exc_val, exc_tb):
    array_ops.placeholder = self._default_placeholder
    ops.name_scope = self._default_name_scope
    base_layer.make_variable = self._default_make_variable
    random_ops.random_normal = self._default_random_normal
    gen_linalg_ops.qr = self._default_qr


class SizedInfeed(collections.namedtuple('SizedInfeed',
                                         ['sharded_infeed_tensors',
                                          'infeed_ops'])):
  """Represents an instantiation of the infeed ops for a concrete input shape.

  sharded_infeed_tensors: A data structure of Tensors used to represent the
    placeholder tensors that must be fed when using feed_dicts.

  infeed_ops: the set of ops that will be run to drive infeed for a single step.
  """
  pass


class TPUInfeedInstance(object):
  """TPUInfeedInstance represents the logic to manage feeding in a single step.

  See the comments on the `TPUInfeedManager` for a description for how infeed
  is managed.
  """

  @abc.abstractmethod
  def make_input_specs(self, input_tensors):
    """Constructs the infeed_specs for the given Infeed instance.

    Args:
      input_tensors: The inputs to the model.

    Returns:
      A list of
    """
    pass

  def make_feed_dict(self, tpu_model_op):
    """Constructs a feed_dict for this instance, given the tpu_model_op.

    Args:
      tpu_model_op: A `TPUModelOp` representing the TPU Model for this
        instance's input spec.

    Returns:
      A dictionary to use as the feed_dict of a `session.run` call.
    """
    pass


class TPUInfeedManager(object):
  """TPUInfeedManager manages the data infeeding of data to a TPU computation.

  Because there are multiple data sources (e.g. in-memory NumPy arrays,
  `tf.data.Dataset`s), we abstract the different logic behind a single
  interface: the `TPUInfeedManager`.

  (1) A `TPUFunction` is called with a set of inputs. Based on the inputs,
  `TPUFunction` retrieves the corresponding `TPUInfeedManager` (or constructs a
  new one if required).

  (2) The `TPUFunction` calls `make_infeed_instance` on the `TPUInfeedManager`
  which returns a `TPUInfeedInstance`.

  (3) The `TPUFunction` checks in the shape cache for a pre-compiled instance of
  the model based on the returned `input_specs` from `TPUInfeedInstance`.

  (4) [Optional.] If the model has not already been instantiated for the given
  input spec, the `TPUFunction` compiles the model for the input spec (using the
  `TPUInfeedManager`).

  (5) The `TPUInfeedInstance` constructs the session.run's feed_dict given the
  compiled model instance corresponding to its shape.
  """

  @abc.abstractmethod
  def make_infeed_instance(self, inputs):
    """Given a single step's input, construct a `TPUInfeedInstance`.

    Args:
      inputs: The inputs to a given step.

    Returns:
      A subclass of `TPUInfeedInstance`.
    """
    pass

  @abc.abstractmethod
  def build_infeed_from_input_specs(self, input_specs, execution_mode):
    """For a given input specification (size, type), construct the infeed ops.

    This is called only once for a given input specification and builds the
    graph ops. It does not have a pointer to the actual infeed data.

    Args:
      input_specs: TODO(saeta): Document me!
      execution_mode: TODO(saeta): Document me!

    Returns:
      A `SizedInfeed` instance.
    """
    pass


class TPUNumpyInfeedManager(TPUInfeedManager):
  """TPU Infeed manager for Numpy inputs."""

  class NumpyInfeedInstance(TPUInfeedInstance):
    """Infeed instance for Numpy inputs."""

    def __init__(self, sharded_inputs):
      self._sharded_inputs = sharded_inputs

    def make_input_specs(self, input_tensors):
      # Compute an input specification (used to generate infeed enqueue and
      # dequeue operations).  We use the shape from our input array and the
      # dtype from our model.  A user may pass in a float64 for a float32
      # input: for model compatibility we still must generate a float32 infeed.
      input_specs = []
      # We use the shape and dtype from the first shard to compute the input
      # metadata (`input_specs`); all replicas have the same type and shape.
      for tensor, ary in zip(input_tensors, self._sharded_inputs[0]):
        input_specs.append(
            tensor_spec.TensorSpec(ary.shape, tensor.dtype,
                                   _valid_name(tensor.name)))

      return input_specs

    def make_feed_dict(self, tpu_model_op):
      infeed_dict = {}
      for infeed_tensors, inputs in zip(tpu_model_op.infeed_tensors,
                                        self._sharded_inputs):
        for tensor, value in zip(infeed_tensors, inputs):
          infeed_dict[tensor] = value
      return infeed_dict

  def __init__(self, tpu_assignment):
    self._tpu_assignment = tpu_assignment

  def _split_tensors(self, inputs):
    """Split input data across shards.

    Each input is sliced along the batch axis.

    Args:
      inputs: List of Numpy arrays to run on the TPU.

    Returns:
      List of lists containing the input to feed to each TPU shard.
    """
    if self._tpu_assignment.num_towers == 1:
      return [inputs]

    batch_size = inputs[0].shape[0]
    assert batch_size % self._tpu_assignment.num_towers == 0, (
        'batch_size must be divisible by the number of TPU cores in use (%s '
        'vs %s)' % (batch_size, self._tpu_assignment.num_towers))
    shard_size = batch_size // self._tpu_assignment.num_towers
    input_list = []
    for index in range(self._tpu_assignment.num_towers):
      shard_inputs = [
          x[index * shard_size:(index + 1) * shard_size] for x in inputs
      ]
      input_list.append(shard_inputs)
    return input_list

  def make_infeed_instance(self, inputs):
    sharded_inputs = self._split_tensors(inputs)
    return self.NumpyInfeedInstance(sharded_inputs)

  def build_infeed_from_input_specs(self, input_specs, execution_mode):
    infeed_op = []
    shard_infeed_tensors = []

    for shard_id in range(self._tpu_assignment.num_towers):
      with ops.device(
          '/job:%s/device:CPU:0' % self._tpu_assignment.worker_name):
        infeed_tensors = []
        with ops.device('/device:TPU:%d' % shard_id):
          for spec in input_specs:
            # Construct placeholders for each of the inputs.
            infeed_tensors.append(
                array_ops.placeholder(
                    dtype=spec.dtype,
                    shape=spec.shape,
                    name='infeed-enqueue-%s-%d' % (spec.name, shard_id)))
        shard_infeed_tensors.append(infeed_tensors)

        infeed_op.append(
            tpu_ops.infeed_enqueue_tuple(
                infeed_tensors, [spec.shape for spec in input_specs],
                name='infeed-enqueue-%s-%d' % (execution_mode, shard_id),
                device_ordinal=shard_id))
    return SizedInfeed(infeed_ops=infeed_op,
                       sharded_infeed_tensors=shard_infeed_tensors)


class TPUDatasetInfeedManager(TPUInfeedManager):
  """Manages infeed for a `tf.data.Dataset` into a TPU computation.
  """

  class DatasetInfeedInstance(TPUInfeedInstance):
    """An instance of the TPU infeed."""

    def __init__(self, input_specs):
      self._input_specs = input_specs

    def make_input_specs(self, input_tensors):
      # TODO(saeta): Do error checking here!
      return self._input_specs

    def make_feed_dict(self, tpu_model_op):
      # TODO(saeta): Verify tpu_model_op is as expected!
      return {}

  # pylint: disable=redefined-outer-name
  def __init__(self, dataset, tpu_assignment, tpu_session):
    """Constructs a TPUDatasetInfeedManager.

    Must be called within a `KerasTPUModel.tpu_session` context!

    Args:
      dataset: A `tf.data.Dataset` to infeed.
      tpu_assignment: The `TPUAssignment` used to configure the
        Keras TPU model.
      tpu_session: The `tf.Session` object used for running the TPU model.
    """
    self._verify_dataset_shape(dataset)
    self._dataset = dataset
    self._tpu_assignment = tpu_assignment
    dummy_x_shape = dataset.output_shapes[0].as_list()
    dummy_x_shape[0] *= tpu_assignment.num_towers
    dummy_y_shape = dataset.output_shapes[1].as_list()
    dummy_y_shape[0] *= tpu_assignment.num_towers
    self._iterator = dataset.make_initializable_iterator()
    tpu_session.run(self._iterator.initializer)

    self._get_next_ops = []
    ctrl_deps = []
    for i in range(tpu_assignment.num_towers):
      with ops.control_dependencies(ctrl_deps):  # Ensure deterministic
        # TODO(saeta): Ensure correct placement!
        get_next_op = self._iterator.get_next()
        self._get_next_ops.append(get_next_op)
        ctrl_deps.extend(get_next_op)

    # Use dummy numpy inputs for the rest of Keras' shape checking. We
    # intercept them when building the model.
    self._dummy_x = np.zeros(dummy_x_shape,
                             dtype=dataset.output_types[0].as_numpy_dtype)
    self._dummy_y = np.zeros(dummy_y_shape,
                             dtype=dataset.output_types[1].as_numpy_dtype)

    input_specs = []
    if isinstance(self._iterator.output_shapes, tuple):
      assert isinstance(self._iterator.output_types, tuple)
      assert len(self._iterator.output_shapes) == len(
          self._iterator.output_types)
      for i in range(len(self._iterator.output_shapes)):
        spec = tensor_spec.TensorSpec(self._iterator.output_shapes[i],
                                      self._iterator.output_types[i])
        input_specs.append(spec)
    elif isinstance(self._iterator.output_shapes, tensor_shape.TensorShape):
      spec = tensor_spec.TensorSpec(self._iterator.output_shapes,
                                    self._iterator.output_types)
      input_specs.append(spec)

    self._infeed_instance = self.DatasetInfeedInstance(input_specs)

  def _verify_dataset_shape(self, dataset):
    """Verifies a dataset is of an appropriate shape for TPUs."""
    if not isinstance(dataset, dataset_ops.Dataset):
      raise ValueError('The function passed as the `x` parameter did not '
                       'return a `tf.data.Dataset`.')
    if not isinstance(dataset.output_classes, tuple):
      raise ValueError('The dataset must return a tuple of tf.Tensors, '
                       'instead it returns: %s' % dataset.output_classes)
    if len(dataset.output_classes) != 2:
      raise ValueError(
          'The dataset must return a 2-element tuple, got '
          '%s output classes instead.' % (dataset.output_classes,))
    for i, cls in enumerate(dataset.output_classes):
      if cls != ops.Tensor:
        raise ValueError('The dataset returned a non-Tensor type (%s) at '
                         'index %d.' % (cls, i))
    for i, shape in enumerate(dataset.output_shapes):
      if not shape:
        raise ValueError('The dataset returns a scalar tensor in '
                         'tuple index %d. Did you forget to batch? '
                         '(Output shapes: %s).' % (i,
                                                   dataset.output_shapes))
      for j, dim in enumerate(shape):
        if dim.value is None:
          if j == 0:
            hint = (' Hint: did you use `ds.batch(BATCH_SIZE, '
                    'drop_remainder=True)`?')
          else:
            hint = ''
          raise ValueError(
              'The Keras-TPU integration for `tf.data` '
              'currently requires static shapes. The provided '
              'dataset only has a partially defined shape. '
              '(Dimension %d of output tensor %d is not statically known '
              'for output shapes: %s.%s)' % (j, i, dataset.output_shapes, hint))

  @property
  def dummy_x(self):
    return self._dummy_x

  @property
  def dummy_y(self):
    return self._dummy_y

  def make_infeed_instance(self, inputs):
    # TODO(saeta): Verify inputs is as expected.
    return self._infeed_instance

  def build_infeed_from_input_specs(self, input_specs, execution_mode):
    shard_infeed_tensors = self._get_next_ops
    assert len(shard_infeed_tensors) == self._tpu_assignment.num_towers
    infeed_ops = []
    for shard_id in range(self._tpu_assignment.num_towers):
      with ops.device(
          '/job:%s/device:CPU:0' % self._tpu_assignment.worker_name):
        infeed_ops.append(
            tpu_ops.infeed_enqueue_tuple(
                shard_infeed_tensors[shard_id],
                [spec.shape for spec in input_specs],
                name='infeed-enqueue-%s-%d' % (execution_mode, shard_id),
                device_ordinal=shard_id))
    return SizedInfeed(infeed_ops=infeed_ops,
                       sharded_infeed_tensors=shard_infeed_tensors)


class TPUFunction(object):
  """K.function compatible interface for invoking a TPU compiled function.

  Recompilation is triggered on-demand for each set of new inputs shapes: the
  results are cached for future execution.  We expect most computations will
  be dominated by a standard batch-size, followed by a straggler batch for
  the end of training or evaluation.

  All `inputs` and `outputs` will be loaded via the infeed and outfeed queues
  instead of being injected as `feed_dict` items or fetches.
  """

  def __init__(self, model, execution_mode, tpu_assignment):
    self.model = model
    self.execution_mode = execution_mode
    self._tpu_assignment = tpu_assignment
    self._compilation_cache = {}
    self._cloned_model = None

    # Copy optimizer configuration.  This is done prior to `_specialize_model`
    # as the configuration may require evaluating variables in the CPU session.
    self._optimizer_config = None
    if not isinstance(self.model.optimizer, keras_optimizers.TFOptimizer):
      self._optimizer_config = self.model.optimizer.get_config()

  def _specialize_model(self, input_specs, infeed_manager):
    """Specialize `self.model` (a Keras model) for the given input shapes."""
    # Re-create our input and output layers inside our subgraph.  They will be
    # attached to the true computation when we clone our model in `tpu_fn`.
    K.set_learning_phase(self.execution_mode == model_fn_lib.ModeKeys.TRAIN)

    # functools.partial and callable objects are not supported by tpu.rewrite
    def _model_fn():
      """Compute fit/eval/predict for the TPU."""
      is_training = self.execution_mode == model_fn_lib.ModeKeys.TRAIN
      is_test = self.execution_mode == model_fn_lib.ModeKeys.EVAL
      is_predict = self.execution_mode == model_fn_lib.ModeKeys.PREDICT

      # During train/eval, we infeed our features as well as labels.
      if is_training or is_test:
        infeed_layers = self.model._input_layers + self.model._output_layers
      else:
        infeed_layers = self.model._input_layers

      # Generate our infeed operation to read features & labels.
      infeed_tensors = tpu_ops.infeed_dequeue_tuple(
          dtypes=[spec.dtype for spec in input_specs],
          shapes=[spec.shape for spec in input_specs],
          name='infeed-%s' % self.execution_mode)

      assert len(infeed_tensors) == len(infeed_layers), (
          'Infeed inputs did not match model: %s vs %s' % (infeed_layers,
                                                           infeed_tensors))

      tpu_targets = []
      tpu_input_map = {}

      # Sort infeed outputs into inputs and labels for calling our Keras model.
      for tensor, layer in zip(infeed_tensors, infeed_layers):
        if layer in self.model._input_layers:
          tpu_input_map[layer.name] = tensor
        if layer in self.model._output_layers:
          tpu_targets.append(tensor)

      # Clone our CPU model, running within the TPU device context.
      with TPURewriteContext(tpu_input_map):
        with variable_scope.variable_scope('tpu_model_%s' % id(self.model)):
          with keras_tpu_variables.replicated_scope(
              self._tpu_assignment.num_towers):
            self._cloned_model = models.clone_model(self.model)

      # Create a copy of the optimizer for this graph.
      if isinstance(self.model.optimizer, keras_optimizers.TFOptimizer):
        cloned_optimizer = keras_optimizers.TFOptimizer(
            self.model.optimizer.optimizer)
      else:
        logging.info('Cloning %s %s', self.model.optimizer.__class__.__name__,
                     self._optimizer_config)
        cloned_optimizer = self.model.optimizer.__class__.from_config(
            self._optimizer_config)

      if is_training or is_test:
        self._cloned_model.compile(
            optimizer=_replicated_optimizer(cloned_optimizer),
            loss=self.model.loss,
            loss_weights=self.model.loss_weights,
            metrics=self.model.metrics,
            weighted_metrics=self.model.weighted_metrics,
            target_tensors=tpu_targets,
        )

      # Compute our outfeed depending on the execution mode
      if is_training:
        self._cloned_model._make_train_function()
        self._outfeed_spec = [
            tensor_spec.TensorSpec(tensor.shape, tensor.dtype, tensor.name)
            for tensor in self._cloned_model.train_function.outputs
        ]
        return [
            self._cloned_model.train_function.updates_op,
            tpu_ops.outfeed_enqueue_tuple(
                self._cloned_model.train_function.outputs,
                name='outfeed-enqueue-train')
        ]
      elif is_test:
        self._cloned_model._make_test_function()
        self._outfeed_spec = [
            tensor_spec.TensorSpec(tensor.shape, tensor.dtype, tensor.name)
            for tensor in self._cloned_model.test_function.outputs
        ]
        return [
            tpu_ops.outfeed_enqueue_tuple(
                self._cloned_model.test_function.outputs,
                name='outfeed-enqueue-test')
        ]
      elif is_predict:
        self._cloned_model._make_predict_function()
        self._outfeed_spec = [
            tensor_spec.TensorSpec(tensor.shape, tensor.dtype, tensor.name)
            for tensor in self._cloned_model.predict_function.outputs
        ]
        return [
            tpu_ops.outfeed_enqueue_tuple(
                self._cloned_model.predict_function.outputs,
                name='outfeed-enqueue-predict',
            )
        ]
      else:
        assert False, 'Unexpected execution mode: %s' % self.execution_mode

    # Capture outfeed metadata computed during the rewrite.
    self._outfeed_spec = None

    # Generate out TPU operations using `tpu.split_compile_and_replicate`.
    # `compile_op` can be used to test the TPU model compiles before execution.
    # `execute op` replicates `_model_fn` `num_replicas` times, with each shard
    # running on a different logical core.
    compile_op, execute_op = tpu.split_compile_and_replicate(
        _model_fn, inputs=[[]] * self._tpu_assignment.num_towers)

    # Generate CPU side operations to enqueue features/labels and dequeue
    # outputs from the model call.
    sized_infeed = infeed_manager.build_infeed_from_input_specs(
        input_specs, self.execution_mode)
    # Build output ops.
    outfeed_op = []
    for shard_id in range(self._tpu_assignment.num_towers):
      with ops.device(
          '/job:%s/device:CPU:0' % self._tpu_assignment.worker_name):
        outfeed_op.extend(
            tpu_ops.outfeed_dequeue_tuple(
                dtypes=[spec.dtype for spec in self._outfeed_spec],
                shapes=[spec.shape for spec in self._outfeed_spec],
                name='outfeed-dequeue-%s-%d' % (self.execution_mode, shard_id),
                device_ordinal=shard_id))

    return TPUModelOp(
        compile_op,
        execute_op,
        infeed_tensors=sized_infeed.sharded_infeed_tensors,
        infeed_op=sized_infeed.infeed_ops,
        outfeed_op=outfeed_op)

  def _test_model_compiles(self, tpu_model_ops):
    """Verifies that the given TPUModelOp can be compiled via XLA."""
    logging.info('Started compiling')
    start_time = time.time()

    result = K.get_session().run(tpu_model_ops.compile_op)
    proto = tpu_compilation_result.CompilationResultProto()
    proto.ParseFromString(result)
    if proto.status_error_message:
      raise RuntimeError('Compilation failed: {}'.format(
          proto.status_error_message))

    end_time = time.time()
    logging.info('Finished compiling. Time elapsed: %s secs',
                 end_time - start_time)

  def _lookup_infeed_manager(self, inputs):
    """Return an existing manager, or construct a new InfeedManager for inputs.

    _lookup_infeed_manager will return an existing InfeedManager if one has been
    previously assigned for this model and input. If not, it will construct a
    new TPUNumpyInfeedManager.

    Args:
      inputs: A NumPy input to the model.

    Returns:
      A `TPUInfeedManager` object to manage infeeds for this input.
    """
    if inputs is None:
      return None

    for x, mgr in self.model._numpy_to_infeed_manager_list:
      if inputs[0] is x:
        return mgr
    return TPUNumpyInfeedManager(self.model._tpu_assignment)

  def _tpu_model_ops_for_input_specs(self, input_specs, infeed_manager):
    """Looks up the corresponding `TPUModelOp` for a given `input_specs`.

    It instantiates a new copy of the model for each unique input shape.

    Args:
      input_specs: The specification of the inputs to train on.
      infeed_manager: The infeed manager responsible for feeding in data.

    Returns:
      A `TPUModelOp` instance that can be used to execute a step of the model.
    """
    if input_specs is None or infeed_manager is None:
      # Note: this condition is possible during the prologue or epilogue of the
      # pipelined loop.
      return None

    # XLA requires every operation in the graph has a fixed shape.  To
    # handle varying batch sizes we recompile a new sub-graph for each
    # unique input shape.
    shape_key = tuple([tuple(spec.shape.as_list()) for spec in input_specs])
    if shape_key not in self._compilation_cache:
      with self.model.tpu_session():
        logging.info('New input shapes; (re-)compiling: mode=%s, %s',
                     self.execution_mode, input_specs)
        new_tpu_model_ops = self._specialize_model(input_specs,
                                                   infeed_manager)
        self._compilation_cache[shape_key] = new_tpu_model_ops
        self._test_model_compiles(new_tpu_model_ops)

    return self._compilation_cache[shape_key]

  def _construct_input_tensors_and_inputs(self, inputs):
    """Returns input tensors and numpy array inputs corresponding to `inputs`.

    Args:
      inputs: NumPy inputs.

    Returns:
      A tuple of `input_tensors`, and `inputs`.
    """
    if inputs is None:
      # Note: this condition is possible during the prologue or epilogue of the
      # pipelined loop.
      return None, None

    if (self.model.uses_learning_phase and
        not isinstance(K.learning_phase(), int)):
      # Remove the learning_phase flag at the end. We currently hard code the
      # learning_phase in TPUFunction.
      assert isinstance(inputs[-1], int), (
          'Expect the final element be learning_phase flag. Got {}'.format(
              inputs[-1]))
      inputs = inputs[:-1]

    if (self.execution_mode == model_fn_lib.ModeKeys.TRAIN or
        self.execution_mode == model_fn_lib.ModeKeys.EVAL):
      # Strip sample weight from inputs.
      input_tensors = self.model._feed_inputs + self.model._feed_targets
    else:
      input_tensors = self.model._feed_inputs

    inputs = inputs[:len(input_tensors)]
    return input_tensors, inputs

  def _process_outputs(self, outfeed_outputs):
    """Processes the outputs of a model function execution.

    Args:
      outfeed_outputs: The sharded outputs of the TPU computation.

    Returns:
      The aggregated outputs of the TPU computation to be used in the rest of
      the model execution.
    """
    # TODO(xiejw): Decide how to reduce outputs, or discard all but first.
    if self.execution_mode == model_fn_lib.ModeKeys.PREDICT:
      outputs = [[]] * len(self._outfeed_spec)
      outputs_per_replica = len(self._outfeed_spec)

      for i in range(self._tpu_assignment.num_towers):
        output_group = outfeed_outputs[i * outputs_per_replica:(i + 1) *
                                       outputs_per_replica]
        for j in range(outputs_per_replica):
          outputs[j].append(output_group[j])

      return [np.concatenate(group) for group in outputs]
    else:
      return outfeed_outputs[:len(outfeed_outputs) //
                             self._tpu_assignment.num_towers]

  def __call__(self, inputs):
    """__call__ executes the function on the computational hardware.

    It handles executing infeed, and preprocessing in addition to executing the
    model on the TPU hardware.

    Note: `__call__` has a sibling method `pipeline_run` which performs the same
    operations, but with software pipelining.

    Args:
      inputs: The inputs to use to train.

    Returns:
      The output of the computation for the given mode it is executed in.

    Raises:
      RuntimeError: If there is an inappropriate use of the function.
    """
    assert isinstance(inputs, list)

    infeed_manager = self._lookup_infeed_manager(inputs)
    input_tensors, inputs = self._construct_input_tensors_and_inputs(inputs)
    infeed_instance = infeed_manager.make_infeed_instance(inputs)
    del inputs  # To avoid accident usage.
    input_specs = infeed_instance.make_input_specs(input_tensors)
    tpu_model_ops = self._tpu_model_ops_for_input_specs(input_specs,
                                                        infeed_manager)
    infeed_dict = infeed_instance.make_feed_dict(tpu_model_ops)

    # Initialize our TPU weights on the first compile.
    self.model._initialize_weights(self._cloned_model)

    with self.model.tpu_session() as session:
      _, _, outfeed_outputs = session.run([
          tpu_model_ops.infeed_op, tpu_model_ops.execute_op,
          tpu_model_ops.outfeed_op
      ], infeed_dict)
    return self._process_outputs(outfeed_outputs)

  def pipeline_run(self, cur_step_inputs, next_step_inputs):
    """pipeline_run executes the function on the computational hardware.

    pipeline_run performs the same computation as __call__, however it runs the
    infeed in a software pipelined fashion compared to the on-device execution.

    Note: it is the responsibility of the caller to call `pipeline_run` in the
    following sequence:
      - Once with `cur_step_inputs=None` and `next_step_inputs=list(...)`
      - `n` times with `cur_step_inputs` and `next_step_inputs` as `list`s
      - Once with `cur_step_inputs=list(...)` and `next_step_inputs=None`
    Additionally, it is the responsibility of the caller to pass
    `next_step_inputs` as `cur_step_inputs` on the next invocation of
    `pipeline_run`.

    Args:
      cur_step_inputs: The current step's inputs.
      next_step_inputs: The next step's inputs.

    Returns:
      The output of the computation for the given mode it is executed in.

    Raises:
      RuntimeError: If there is an inappropriate use of the function.
    """
    # Software pipelined case.
    next_step_infeed_manager = self._lookup_infeed_manager(next_step_inputs)
    cur_step_infeed_manager = self._lookup_infeed_manager(cur_step_inputs)

    if (next_step_infeed_manager is not None
        and cur_step_infeed_manager is not None):
      assert type(next_step_infeed_manager) is type(cur_step_infeed_manager)

    next_input_tensors, next_step_inputs = (
        self._construct_input_tensors_and_inputs(next_step_inputs))
    cur_input_tensors, cur_step_inputs = (
        self._construct_input_tensors_and_inputs(cur_step_inputs))

    cur_infeed_instance = None
    if cur_step_infeed_manager:
      cur_infeed_instance = cur_step_infeed_manager.make_infeed_instance(
          cur_step_inputs)
    next_infeed_instance = None
    if next_step_infeed_manager:
      next_infeed_instance = next_step_infeed_manager.make_infeed_instance(
          next_step_inputs)

    del cur_step_inputs  # Avoid accidental re-use.
    del next_step_inputs  # Avoid accidental re-use.

    cur_tpu_model_ops = None
    next_tpu_model_ops = None
    infeed_dict = None

    if cur_infeed_instance and cur_input_tensors and cur_step_infeed_manager:
      cur_input_specs = cur_infeed_instance.make_input_specs(
          cur_input_tensors)
      cur_tpu_model_ops = self._tpu_model_ops_for_input_specs(
          cur_input_specs, cur_step_infeed_manager)

    if (next_infeed_instance
        and next_input_tensors
        and next_step_infeed_manager):
      next_input_specs = next_infeed_instance.make_input_specs(
          next_input_tensors)
      next_tpu_model_ops = self._tpu_model_ops_for_input_specs(
          next_input_specs, next_step_infeed_manager)
      infeed_dict = next_infeed_instance.make_feed_dict(next_tpu_model_ops)

    # Initialize our TPU weights on the first compile.
    self.model._initialize_weights(self._cloned_model)

    if next_tpu_model_ops and cur_tpu_model_ops:
      with self.model.tpu_session() as session:
        _, _, outfeed_outputs = session.run([
            next_tpu_model_ops.infeed_op, cur_tpu_model_ops.execute_op,
            cur_tpu_model_ops.outfeed_op
        ], infeed_dict)
      return self._process_outputs(outfeed_outputs)
    if cur_tpu_model_ops:
      with self.model.tpu_session() as session:
        _, outfeed_outputs = session.run([
            cur_tpu_model_ops.execute_op, cur_tpu_model_ops.outfeed_op])
      return self._process_outputs(outfeed_outputs)
    if next_tpu_model_ops:
      with self.model.tpu_session() as session:
        session.run(next_tpu_model_ops.infeed_op, infeed_dict)
      return None
    raise RuntimeError('Internal error: both current & next tpu_model_ops '
                       'were None')



class KerasTPUModel(models.Model):
  """TPU compatible Keras model wrapper."""

  def __init__(self, cpu_model, strategy):
    super(models.Model, self).__init__(  # pylint: disable=bad-super-call
        inputs=cpu_model.inputs,
        outputs=cpu_model.outputs,
        name=cpu_model.name,
    )

    # Create a mapping from numpy arrays to infeed managers.
    # Note: uses a list of tuples instead of a map because numpy arrays are
    # not hashable.
    self._numpy_to_infeed_manager_list = []

    self.predict_function = None
    self.test_function = None
    self.train_function = None

    cluster_resolver = strategy._tpu_cluster_resolver
    self._tpu_name_or_address = cluster_resolver.get_master()
    self._cpu_model = cpu_model
    self._tpu_assignment = strategy._make_assignment_for_model(cpu_model)
    self._tpu_model = None
    self._tpu_weights_initialized = False

    self._session = tpu_session(cluster_resolver)

    # If the input CPU model has already been compiled, compile our TPU model
    # immediately.
    if self._cpu_model.optimizer:
      self.compile(
          self._cpu_model.optimizer,
          self._cpu_model.loss,
          self._cpu_model.metrics,
          self._cpu_model.loss_weights,
          self._cpu_model.sample_weight_mode,
          self._cpu_model.weighted_metrics,
          self._cpu_model.target_tensors,
      )

  def get_config(self):
    return {
        'cpu_model': self._cpu_model,
        'tpu_name_or_address': self._tpu_name_or_address,
        'tpu_assignment': self._tpu_assignment,
    }

  def compile(self,
              optimizer,
              loss=None,
              metrics=None,
              loss_weights=None,
              sample_weight_mode=None,
              weighted_metrics=None,
              target_tensors=None,
              **kwargs):
    if sample_weight_mode:
      raise ValueError('sample_weight_mode not supported for TPU execution.')
    if weighted_metrics:
      raise ValueError('weighted_metrics not supported for TPU execution.')
    if target_tensors:
      raise ValueError('target_tensors is not supported for TPU execution.')

    super(KerasTPUModel, self).compile(optimizer, loss, metrics, loss_weights,
                                       sample_weight_mode, weighted_metrics,
                                       target_tensors, **kwargs)

    if not self._cpu_model.optimizer:
      self._cpu_model.compile(optimizer, loss, metrics, loss_weights,
                              sample_weight_mode, weighted_metrics,
                              target_tensors, **kwargs)

  def fit(self,
          x=None,
          y=None,
          batch_size=None,
          epochs=1,
          verbose=1,
          callbacks=None,
          validation_split=0.,
          validation_data=None,
          shuffle=True,
          class_weight=None,
          sample_weight=None,
          initial_epoch=0,
          steps_per_epoch=None,
          validation_steps=None,
          **kwargs):
    if context.executing_eagerly():
      raise EnvironmentError('KerasTPUModel currently does not support eager '
                             'mode.')

    assert not self._numpy_to_infeed_manager_list  # Ensure empty.

    infeed_managers = []  # Managers to clean up at the end of the fit call.
    if isinstance(x, dataset_ops.Dataset):
      # TODO(b/111413240): Support taking a tf.data.Dataset directly.
      raise ValueError(
          'Taking a Dataset directly is not yet supported. Please '
          'wrap your dataset construction code in a function and '
          'pass that to fit instead. For examples, see: '
          'https://github.com/tensorflow/tpu/tree/master/models/experimental'
          '/keras')
    if callable(x):
      with self.tpu_session() as sess,\
          ops.device('/job:%s/device:CPU:0' % self._tpu_assignment.worker_name):
        dataset = x()
        if steps_per_epoch is None:
          raise ValueError('When using tf.data as input to a model, you '
                           'should specify the steps_per_epoch argument.')
        if y is not None:
          raise ValueError('When using tf.data as input to a model, y must be '
                           'None')
        infeed_manager = TPUDatasetInfeedManager(dataset, self._tpu_assignment,
                                                 sess)
        # Use dummy numpy inputs for the rest of Keras' shape checking. We
        # intercept them when building the model.
        x = infeed_manager.dummy_x
        y = infeed_manager.dummy_y
        infeed_managers.append((x, infeed_manager))

    if isinstance(validation_data, dataset_ops.Dataset):
      # TODO(b/111413240): Support taking a tf.data.Dataset directly.
      raise ValueError(
          'Taking a Dataset directly is not yet supported. Please '
          'wrap your dataset construction code in a function and '
          'pass that to fit instead. For examples, see: '
          'https://github.com/tensorflow/tpu/tree/master/models/experimental'
          '/keras')
    if callable(validation_data):
      with self.tpu_session() as sess:
        dataset = validation_data()
        if validation_steps is None:
          raise ValueError('When using tf.data as validation for a model, you '
                           'should specify the validation_steps argument.')
        infeed_manager = TPUDatasetInfeedManager(dataset, self._tpu_assignment,
                                                 sess)
        # Use dummy numpy inputs for the rest of Keras' shape checking. We
        # intercept them when building the model.
        val_x = infeed_manager.dummy_x
        val_y = infeed_manager.dummy_y
        infeed_managers.append((val_x, infeed_manager))
        validation_data = (val_x, val_y)

    self._numpy_to_infeed_manager_list = infeed_managers
    try:
      if not kwargs.get('_pipeline', True):
        logging.info(
            'Running non-pipelined training loop (`_pipeline=%s`).',
            kwargs['_pipeline'])
        kwargs.pop('_pipeline')
        return super(KerasTPUModel, self).fit(
            x,
            y,
            batch_size,
            epochs,
            verbose,
            callbacks,
            validation_split,
            validation_data,
            shuffle,
            class_weight,
            sample_weight,
            initial_epoch,
            steps_per_epoch,
            validation_steps,
            **kwargs)
      return self._pipeline_fit(
          x,
          y,
          batch_size,
          epochs,
          verbose,
          callbacks,
          validation_split,
          validation_data,
          shuffle,
          class_weight,
          sample_weight,
          initial_epoch,
          steps_per_epoch,
          validation_steps,
          **kwargs)
    finally:
      self._numpy_to_infeed_manager_list = []

  def evaluate(self,
               x=None,
               y=None,
               batch_size=None,
               verbose=1,
               sample_weight=None,
               steps=None):
    assert not self._numpy_to_infeed_manager_list  # Ensure empty.

    infeed_managers = []  # Managers to clean up at the end of the fit call.
    if isinstance(x, dataset_ops.Dataset):
      # TODO(b/111413240): Support taking a tf.data.Dataset directly.
      raise ValueError(
          'Taking a Dataset directly is not yet supported. Please '
          'wrap your dataset construction code in a function and '
          'pass that to fit instead. For examples, see: '
          'https://github.com/tensorflow/tpu/tree/master/models/experimental'
          '/keras')
    if callable(x):
      with self.tpu_session() as sess:
        dataset = x()
        if steps is None:
          raise ValueError('When using tf.data as input to a model, you '
                           'should specify the steps argument.')
        if y is not None:
          raise ValueError('When using tf.data as input to a model, y must be '
                           'None')
        infeed_manager = TPUDatasetInfeedManager(dataset, self._tpu_assignment,
                                                 sess)
        # Use dummy numpy inputs for the rest of Keras' shape checking. We
        # intercept them when building the model.
        x = infeed_manager.dummy_x
        y = infeed_manager.dummy_y
        infeed_managers.append((x, infeed_manager))

    self._numpy_to_infeed_manager_list = infeed_managers
    try:
      return super(KerasTPUModel, self).evaluate(
          x,
          y,
          batch_size,
          verbose,
          sample_weight,
          steps)
    finally:
      self._numpy_to_infeed_manager_list = []

  def _pipeline_fit(self,
                    x,
                    y,
                    batch_size,
                    epochs,
                    verbose,
                    callbacks,
                    validation_split,
                    validation_data,
                    shuffle,
                    class_weight,
                    sample_weight,
                    initial_epoch,
                    steps_per_epoch,
                    validation_steps,
                    **kwargs):
    # Similar to super.fit(...), but modified to support software pipelining.

    # Backwards compatibility
    if batch_size is None and steps_per_epoch is None:
      batch_size = 32
    # Legacy support
    if 'nb_epoch' in kwargs:
      logging.warning('The `nb_epoch` argument in `fit` has been renamed '
                      '`epochs`.')
      epochs = kwargs.pop('nb_epoch')
    if kwargs:
      raise TypeError('Unrecognized keyword arguments: ' + str(kwargs))

    # Validate and standardize user data
    x, y, sample_weights = self._standardize_user_data(
        x,
        y,
        sample_weight=sample_weight,
        class_weight=class_weight,
        batch_size=batch_size,
        check_steps=True,
        steps_name='steps_per_epoch',
        steps=steps_per_epoch,
        validation_split=validation_split)

    # Prepare validation data
    val_x, val_y, val_sample_weights = self._prepare_validation_data(
        validation_data,
        validation_split,
        validation_steps,
        x,
        y,
        sample_weights,
        batch_size)
    return self._pipeline_fit_loop(
        x,
        y,
        sample_weights=sample_weights,
        batch_size=batch_size,
        epochs=epochs,
        verbose=verbose,
        callbacks=callbacks,
        val_inputs=val_x,
        val_targets=val_y,
        val_sample_weights=val_sample_weights,
        shuffle=shuffle,
        initial_epoch=initial_epoch,
        steps_per_epoch=steps_per_epoch,
        validation_steps=validation_steps)

  def _pipeline_fit_loop(self,
                         inputs,
                         targets,
                         sample_weights,
                         batch_size,
                         epochs,
                         verbose,
                         callbacks,
                         val_inputs,
                         val_targets,
                         val_sample_weights,
                         shuffle,
                         initial_epoch,
                         steps_per_epoch,
                         validation_steps):
    self._make_train_function()
    sample_weights = sample_weights or []
    val_sample_weights = val_sample_weights or []
    if self.uses_learning_phase and not isinstance(K.learning_phase(), int):
      ins = inputs + targets + sample_weights + [1]
    else:
      ins = inputs + targets + sample_weights

    do_validation = False
    if val_inputs:
      do_validation = True
      if (steps_per_epoch is None and verbose and inputs and
          hasattr(inputs[0], 'shape') and hasattr(val_inputs[0], 'shape')):
        print('Train on %d samples, validate on %d samples' %
              (inputs[0].shape[0], val_inputs[0].shape[0]))

    if validation_steps:
      do_validation = True
      if steps_per_epoch is None:
        raise ValueError('Can only use `validation_steps` when doing step-wise '
                         'training, i.e. `steps_per_epoch` must be set.')

    num_training_samples = training_utils.check_num_samples(
        ins, batch_size, steps_per_epoch, 'steps_per_epoch')
    count_mode = 'steps' if steps_per_epoch else 'samples'
    callbacks = cbks.configure_callbacks(
        callbacks,
        self,
        do_validation=do_validation,
        val_inputs=val_inputs,
        val_targets=val_targets,
        val_sample_weights=val_sample_weights,
        batch_size=batch_size,
        epochs=epochs,
        steps_per_epoch=steps_per_epoch,
        samples=num_training_samples,
        validation_steps=validation_steps,
        verbose=verbose,
        count_mode=count_mode)

    if num_training_samples is not None:
      index_array = np.arange(num_training_samples)

    # To prevent a slowdown, we find beforehand the arrays that need conversion.
    feed = self._feed_inputs + self._feed_targets + self._feed_sample_weights
    indices_for_conversion_to_dense = []
    for i in range(len(feed)):
      if issparse is not None and issparse(ins[i]) and not K.is_sparse(feed[i]):
        indices_for_conversion_to_dense.append(i)

    callbacks.on_train_begin()
    for epoch in range(initial_epoch, epochs):
      # Reset stateful metrics
      for m in self.stateful_metric_functions:
        m.reset_states()
      # Update callbacks
      callbacks.on_epoch_begin(epoch)
      epoch_logs = {}
      if steps_per_epoch is not None:
        # Step-wise fit loop.
        self._pipeline_fit_loop_step_wise(
            ins=ins,
            callbacks=callbacks,
            steps_per_epoch=steps_per_epoch,
            epochs=epochs,
            do_validation=do_validation,
            val_inputs=val_inputs,
            val_targets=val_targets,
            val_sample_weights=val_sample_weights,
            validation_steps=validation_steps,
            epoch_logs=epoch_logs)
      else:
        # Sample-wise fit loop.
        self._pipeline_fit_loop_sample_wise(
            ins=ins,
            callbacks=callbacks,
            index_array=index_array,
            shuffle=shuffle,
            batch_size=batch_size,
            num_training_samples=num_training_samples,
            indices_for_conversion_to_dense=indices_for_conversion_to_dense,
            do_validation=do_validation,
            val_inputs=val_inputs,
            val_targets=val_targets,
            val_sample_weights=val_sample_weights,
            validation_steps=validation_steps,
            epoch_logs=epoch_logs)

      callbacks.on_epoch_end(epoch, epoch_logs)
      if callbacks.model.stop_training:
        break
    callbacks.on_train_end()
    return self.history

  def _pipeline_fit_loop_sample_wise(self,
                                     ins,
                                     callbacks,
                                     index_array,
                                     shuffle,
                                     batch_size,
                                     num_training_samples,
                                     indices_for_conversion_to_dense,
                                     do_validation,
                                     val_inputs,
                                     val_targets,
                                     val_sample_weights,
                                     validation_steps,
                                     epoch_logs):
    f = self.train_function
    if shuffle == 'batch':
      index_array = training_utils.batch_shuffle(index_array, batch_size)
    elif shuffle:
      np.random.shuffle(index_array)
    batches = make_batches(num_training_samples, batch_size)

    ins_last_batch = None
    last_batch_logs = None
    batch_index = 0

    for batch_index, (batch_start, batch_end) in enumerate(batches):
      batch_ids = index_array[batch_start:batch_end]
      try:
        if isinstance(ins[-1], int):
          # Do not slice the training phase flag.
          ins_batch = slice_arrays(ins[:-1], batch_ids) + [ins[-1]]
        else:
          ins_batch = slice_arrays(ins, batch_ids)
      except TypeError:
        raise TypeError('TypeError while preparing batch. If using HDF5 '
                        'input data, pass shuffle="batch".')

      # Pipeline batch logs
      next_batch_logs = {}
      next_batch_logs['batch'] = batch_index
      next_batch_logs['size'] = len(batch_ids)
      if batch_index > 0:
        # Callbacks operate one step behind in software pipeline.
        callbacks.on_batch_begin(batch_index - 1, last_batch_logs)
      for i in indices_for_conversion_to_dense:
        ins_batch[i] = ins_batch[i].toarray()

      outs = f.pipeline_run(cur_step_inputs=ins_last_batch,
                            next_step_inputs=ins_batch)
      ins_last_batch = ins_batch

      if batch_index == 0:
        assert outs is None
      else:
        if not isinstance(outs, list):
          outs = [outs]
        for l, o in zip(self.metrics_names, outs):
          last_batch_logs[l] = o  # pylint: disable=unsupported-assignment-operation
        callbacks.on_batch_end(batch_index - 1, last_batch_logs)
        if callbacks.model.stop_training:
          return
      last_batch_logs = next_batch_logs

    # Final batch
    callbacks.on_batch_begin(batch_index, last_batch_logs)
    outs = f.pipeline_run(cur_step_inputs=ins_last_batch, next_step_inputs=None)
    if not isinstance(outs, list):
      outs = [outs]
    for l, o in zip(self.metrics_names, outs):
      last_batch_logs[l] = o
    callbacks.on_batch_end(batch_index, last_batch_logs)
    if callbacks.model.stop_training:
      return

    if do_validation:
      val_outs = training_arrays.test_loop(
          self,
          val_inputs,
          val_targets,
          sample_weights=val_sample_weights,
          batch_size=batch_size,
          steps=validation_steps,
          verbose=0)
      if not isinstance(val_outs, list):
        val_outs = [val_outs]
      # Same labels assumed.
      for l, o in zip(self.metrics_names, val_outs):
        epoch_logs['val_' + l] = o

  def _pipeline_fit_loop_step_wise(self,
                                   ins,
                                   callbacks,
                                   steps_per_epoch,
                                   epochs,
                                   do_validation,
                                   val_inputs,
                                   val_targets,
                                   val_sample_weights,
                                   validation_steps,
                                   epoch_logs):
    f = self.train_function

    # Loop prologue
    try:
      outs = f.pipeline_run(cur_step_inputs=None, next_step_inputs=ins)
      assert outs is None  # Function shouldn't return anything!
    except errors.OutOfRangeError:
      logging.warning('Your dataset iterator ran out of data on the first step '
                      'of the epoch, preventing further training. Check to '
                      'make sure your paths are correct and you have '
                      'permissions to read the files. Skipping validation')

    for step_index in range(steps_per_epoch):
      batch_logs = {'batch': step_index, 'size': 1}
      callbacks.on_batch_begin(step_index, batch_logs)
      try:
        if step_index < steps_per_epoch - 1:
          next_step_inputs = ins
        else:
          next_step_inputs = None
        outs = f.pipeline_run(cur_step_inputs=ins,
                              next_step_inputs=next_step_inputs)
      except errors.OutOfRangeError:
        logging.warning('Your dataset iterator ran out of data; '
                        'interrupting training. Make sure that your '
                        'dataset can generate at least `steps_per_batch * '
                        'epochs` batches (in this case, %d batches). You '
                        'may need to use the repeat() function when '
                        'building your dataset.' % steps_per_epoch * epochs)
        break

      if not isinstance(outs, list):
        outs = [outs]
      for l, o in zip(self.metrics_names, outs):
        batch_logs[l] = o

      callbacks.on_batch_end(step_index, batch_logs)
      if callbacks.model.stop_training:
        break

    if do_validation:
      val_outs = training_arrays.test_loop(self,
                                           val_inputs,
                                           val_targets,
                                           sample_weights=val_sample_weights,
                                           steps=validation_steps,
                                           verbose=0)
      if not isinstance(val_outs, list):
        val_outs = [val_outs]
      # Same labels assumed.
      for l, o in zip(self.metrics_names, val_outs):
        epoch_logs['val_' + l] = o

  def _prepare_validation_data(self,
                               validation_data,
                               validation_split,
                               validation_steps,
                               x,
                               y,
                               sample_weights,
                               batch_size):
    """Prepares the validation dataset.

    Args:
      validation_data: The validation data (if provided)
      validation_split: The validation split (if provided)
      validation_steps: The validation steps (if provided)
      x: The main training data x (if provided)
      y: The main training data y (if provided)
      sample_weights: The sample weights (if provided)
      batch_size: The training batch size (if provided)

    Returns:
      A 3-tuple of (val_x, val_y, val_sample_weights).

    Raises:
      ValueError: If the provided arguments are not compatible with
        `KerasTPUModel`.
    """
    # Note: this is similar to a section of $tf/python/keras/engine/training.py
    # It differns in that tf.data objects are not allowed to be passed directly.
    # Additionally, it handles validating shapes & types appropriately for use
    # in TPUs.
    if validation_data:
      if (isinstance(validation_data, iterator_ops.Iterator) or
          isinstance(validation_data, iterator_ops.EagerIterator) or
          isinstance(validation_data, dataset_ops.Dataset)):
        raise ValueError('KerasTPUModel cannot handle a Dataset or Iterator '
                         'for validation_data. Please instead pass a function '
                         'that returns a `tf.data.Dataset`.')
      if len(validation_data) == 2:
        val_x, val_y = validation_data  # pylint: disable=unpacking-non-sequence
        val_sample_weight = None
      elif len(validation_data) == 3:
        val_x, val_y, val_sample_weight = validation_data  # pylint: disable=unpacking-non-sequence
      else:
        raise ValueError('When passing a `validation_data` argument, it must '
                         'contain either 2 items (x_val, y_val), or 3 items '
                         '(x_val, y_val, val_sample_weights). However we '
                         'received `validation_data=%s`' % validation_data)
      val_x, val_y, val_sample_weights = self._standardize_user_data(
          val_x,
          val_y,
          sample_weight=val_sample_weight,
          batch_size=batch_size,
          steps=validation_steps)
    elif validation_split and 0. < validation_split < 1.:
      if training_utils.has_symbolic_tensors(x):
        raise ValueError('If your data is in the form of symbolic tensors, you '
                         'cannot use `validation_split`.')
      if hasattr(x[0], 'shape'):
        split_at = int(x[0].shape[0] * (1. - validation_split))
      else:
        split_at = int(len(x[0]) * (1. - validation_split))

      x, val_x = (slice_arrays(x, 0, split_at), slice_arrays(x, split_at))
      y, val_y = (slice_arrays(y, 0, split_at), slice_arrays(y, split_at))
      sample_weights, val_sample_weights = (slice_arrays(
          sample_weights, 0, split_at), slice_arrays(sample_weights, split_at))
    elif validation_steps:
      val_x = []
      val_y = []
      val_sample_weights = []
    else:
      val_x = None
      val_y = None
      val_sample_weights = None

    return val_x, val_y, val_sample_weights

  def _make_train_function(self):
    if not self.train_function:
      self.train_function = TPUFunction(
          self,
          model_fn_lib.ModeKeys.TRAIN,
          tpu_assignment=self._tpu_assignment)

    return self.train_function

  def _make_test_function(self):
    if not self.test_function:
      self.test_function = TPUFunction(
          self, model_fn_lib.ModeKeys.EVAL, tpu_assignment=self._tpu_assignment)
    return self.test_function

  def _make_predict_function(self):
    if not self.predict_function:
      self.predict_function = TPUFunction(
          self,
          model_fn_lib.ModeKeys.PREDICT,
          tpu_assignment=self._tpu_assignment)
    return self.predict_function

  def _initialize_weights(self, cloned_model):
    """Initialize TPU weights.

    This is called on the first compile of the TPU model (first call to
    fit/predict/evaluate).

    Args:
      cloned_model: `keras.Model`, TPU model to initialize.
    """
    if self._tpu_weights_initialized:
      return

    self._tpu_model = cloned_model
    self._tpu_weights_initialized = True

    weights = self._cpu_model.get_weights()
    with self.tpu_session():
      logging.info('Setting weights on TPU model.')
      cloned_model.set_weights(weights)

  def sync_to_cpu(self):
    """Copy weights from the CPU, returning a synchronized CPU model."""
    if self._tpu_weights_initialized:
      with self.tpu_session():
        logging.info('Copying TPU weights to the CPU')
        tpu_weights = self._tpu_model.get_weights()

      self._cpu_model.set_weights(tpu_weights)

    return self._cpu_model

  def get_weights(self):
    return self.sync_to_cpu().get_weights()

  def save_weights(self, *args, **kw):
    return self.sync_to_cpu().save_weights(*args, **kw)

  def save(self, *args, **kw):
    return self.sync_to_cpu().save(*args, **kw)

  def set_weights(self, weights):
    # We may not have a TPU model available if we haven't run fit/predict, so
    # we can't directly set the TPU weights here.
    # Instead, reset CPU model weights and force TPU re-initialization at the
    # next call.
    self._cpu_model.set_weights(weights)
    self._tpu_weights_initialized = False

  @contextlib.contextmanager
  def tpu_session(self):
    """Yields a TPU session and sets it as the default Keras session."""
    with self._session.graph.as_default():
      default_session = K.get_session()
      # N.B. We have to call `K.set_session()` AND set our session as the
      # TF default. `K.get_session()` surprisingly does not return the value
      # supplied by K.set_session otherwise.
      K.set_session(self._session)
      with self._session.as_default():
        yield self._session
      K.set_session(default_session)

  def shutdown(self):
    # TODO(b/111364423): Actually shut down the system.
    logging.info('Skipping shutting down TPU system.')
    # with self.tpu_session() as session:
    #   session.run(tpu.shutdown_system())
    self._session.close()


# pylint: disable=bad-continuation
def _validate_shapes(model):
  """Validate that all layers in `model` have constant shape."""
  for layer in model.layers:
    if isinstance(layer.input_shape, tuple):
      input_shapes = [layer.input_shape]
    else:
      input_shapes = layer.input_shape

    if isinstance(layer.output_shape, tuple):
      output_shapes = [layer.output_shape]
    else:
      output_shapes = layer.output_shape

    for shape in input_shapes + output_shapes:
      for dim in shape[1:]:
        if dim is None:
          raise ValueError(
              """
Layer %(layer)s has a variable shape in a non-batch dimension.  TPU models must
have constant shapes for all operations.

You may have to specify `input_length` for RNN/TimeDistributed layers.

Layer: %(layer)s
Input shape: %(input_shape)s
Output shape: %(output_shape)s
  """ % {
          'layer': layer,
          'input_shape': layer.input_shape,
          'output_shape': layer.output_shape
          })


# pylint: enable=bad-continuation


@experimental
def tpu_model(model, strategy=None):
  """Copy `model` along with weights to the TPU.  Returns a TPU model.

  Usage:
  ```
  a = Input(shape=(32,))
  b = Dense(32)(a)
  model = Model(inputs=a, outputs=b)

  # If `num_cores_per_host` is greater than one, batch parallelism will be used
  # to run on multiple TPU cores.
  strategy = keras_support.TPUDistributionStrategy(tpu_cluster_resolver)
  model = keras_support.tpu_model(model, strategy)
  model.compile(
      optimizer=tf.train.GradientDescentOptimizer(learning_rate=1.0),
      ...)
  model.shutdown()
  ```

  Args:
    model: A `KerasTPUModel`.
    strategy: `TPUDistributionStrategy`.  The strategy to use for replicating
              model across multiple TPU cores.

  Returns:
    A new `KerasTPUModel` instance.
  """
  # Force initialization of the CPU model.
  model.get_weights()
  model.reset_states()

  _validate_shapes(model)
  # TODO(xiejw): Validate TPU model. TPUModel only?
  # TODO(xiejw): Validate replicas. Full or 1. Shall we allow subset?
  # TODO(xiejw): Adds reduction option.

  if strategy is None:
    strategy = TPUDistributionStrategy()
  else:
    if not isinstance(strategy, TPUDistributionStrategy):
      raise TypeError(
          '`strategy` must have type `tf.contrib.tpu.TPUDistributionStrategy`. '
          'Got: {}'.format(type(strategy)))

  return KerasTPUModel(cpu_model=model, strategy=strategy)
