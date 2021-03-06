# Copyright 2020 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Differential privacy model.
"""
from easydict import EasyDict as edict

import mindspore as ms
from mindspore.train.model import Model
from mindspore._checkparam import Validator as validator
from mindspore._checkparam import Rel
from mindspore.train import amp
from mindspore.train.amp import _config_level
from mindspore.common import dtype as mstype
from mindspore.nn.wrap.cell_wrapper import _VirtualDatasetCell
from mindspore.parallel._utils import _get_parallel_mode
from mindspore.train.model import ParallelMode
from mindspore.train.amp import _do_keep_batchnorm_fp32
from mindspore.train.amp import _add_loss_network
from mindspore import context
from mindspore import nn
from mindspore import Tensor
from mindspore.ops import composite as C
from mindspore.ops import operations as P
from mindspore.ops import functional as F
from mindspore.ops.operations import NPUGetFloatStatus
from mindspore.ops.operations import NPUAllocFloatStatus
from mindspore.ops.operations import NPUClearFloatStatus
from mindspore.ops.operations import ReduceSum
from mindspore.ops.operations import LessEqual
from mindspore.ops.operations import ControlDepend
from mindspore.parallel._utils import _get_mirror_mean
from mindspore.parallel._utils import _get_device_num
from mindspore.nn.wrap.grad_reducer import DistributedGradReducer
from mindspore.common.parameter import Parameter
from mindspore.nn.wrap.loss_scale import _grad_overflow
from mindspore.nn import Cell
from mindspore import ParameterTuple


GRADIENT_CLIP_TYPE = 1
grad_scale = C.MultitypeFuncGraph("grad_scale")
reciprocal = P.Reciprocal()


@grad_scale.register("Tensor", "Tensor")
def tensor_grad_scale(scale, grad):
    return grad*reciprocal(scale)


class DPModel(Model):
    """
    This class is overload mindspore.train.model.Model.

    Args:
        micro_batches (int): The number of small batches split from an origianl batch. Default: None.
        norm_clip (float): Use to clip the bound, if set 1, will retun the original data. Default: None.
        dp_mech (Mechanisms): The object can generate the different type of noise. Default: None.

    Examples:
        >>> class Net(nn.Cell):
        >>>     def __init__(self):
        >>>         super(Net, self).__init__()
        >>>         self.conv = nn.Conv2d(3, 64, 3, has_bias=False, weight_init='normal')
        >>>         self.bn = nn.BatchNorm2d(64)
        >>>         self.relu = nn.ReLU()
        >>>         self.flatten = nn.Flatten()
        >>>         self.fc = nn.Dense(64*224*224, 12) # padding=0
        >>>
        >>>     def construct(self, x):
        >>>         x = self.conv(x)
        >>>         x = self.bn(x)
        >>>         x = self.relu(x)
        >>>         x = self.flatten(x)
        >>>         out = self.fc(x)
        >>>         return out
        >>>
        >>> net = Net()
        >>> loss = nn.SoftmaxCrossEntropyWithLogits(is_grad=False, sparse=True)
        >>> optim = Momentum(params=net.trainable_params(), learning_rate=0.1, momentum=0.9)
        >>> gaussian_mech = DPOptimizerClassFactory()
        >>> gaussian_mech.set_mechanisms('Gaussian',
        >>>                             norm_bound=args.l2_norm_bound,
        >>>                             initial_noise_multiplier=args.initial_noise_multiplier)
        >>> model = DPModel(micro_batches=2,
        >>>                 norm_clip=1,
        >>>                 dp_mech=gaussian_mech.mech,
        >>>                 network=net,
        >>>                 loss_fn=loss,
        >>>                 optimizer=optim,
        >>>                 metrics=None)
        >>> dataset = get_dataset()
        >>> model.train(2, dataset)
    """
    def __init__(self, micro_batches=None, norm_clip=None, dp_mech=None, **kwargs):
        if micro_batches:
            self._micro_batches = int(micro_batches)
        else:
            self._micro_batches = None
        self._norm_clip = norm_clip
        self._dp_mech = dp_mech
        super(DPModel, self).__init__(**kwargs)

    def amp_build_train_network(self, network, optimizer, loss_fn=None, level='O0', **kwargs):
        """
        Build the mixed precision training cell automatically.

        Args:
            network (Cell): Definition of the network.
            loss_fn (Union[None, Cell]): Definition of the loss_fn. If None, the `network` should have the loss inside.
                Default: None.
            optimizer (Optimizer): Optimizer to update the Parameter.
            level (str): Supports [O0, O2]. Default: "O0".

                - O0: Do not change.
                - O2: Cast network to float16, keep batchnorm and `loss_fn` (if set) run in float32,
                  using dynamic loss scale.

            cast_model_type (:class:`mindspore.dtype`): Supports `mstype.float16` or `mstype.float32`.
                If set to `mstype.float16`, use `float16` mode to train. If set, overwrite the level setting.
            keep_batchnorm_fp32 (bool): Keep Batchnorm run in `float32`. If set, overwrite the level setting.
            loss_scale_manager (Union[None, LossScaleManager]): If None, not scale the loss, or else
                scale the loss by LossScaleManager. If set, overwrite the level setting.
        """
        validator.check_value_type('network', network, nn.Cell, None)
        validator.check_value_type('optimizer', optimizer, nn.Optimizer, None)
        validator.check('level', level, "", ['O0', 'O2'], Rel.IN, None)
        self._check_kwargs(kwargs)
        config = dict(_config_level[level], **kwargs)
        config = edict(config)

        if config.cast_model_type == mstype.float16:
            network.to_float(mstype.float16)

            if config.keep_batchnorm_fp32:
                _do_keep_batchnorm_fp32(network)

        if loss_fn:
            network = _add_loss_network(network, loss_fn, config.cast_model_type)

        if _get_parallel_mode() in (ParallelMode.SEMI_AUTO_PARALLEL, ParallelMode.AUTO_PARALLEL):
            network = _VirtualDatasetCell(network)

        loss_scale = 1.0
        if config.loss_scale_manager is not None:
            loss_scale_manager = config.loss_scale_manager
            loss_scale = loss_scale_manager.get_loss_scale()
            update_cell = loss_scale_manager.get_update_cell()
            if update_cell is not None:
                # only cpu not support `TrainOneStepWithLossScaleCell` for control flow.
                if not context.get_context("enable_ge") and context.get_context("device_target") == "CPU":
                    raise ValueError("Only `loss_scale_manager=None` and "
                                     "`loss_scale_manager=FixedLossScaleManager(drop_overflow_update=False)`"
                                     "are supported in current version. If you use `O2` option, please"
                                     "use `loss_scale_manager=None` or `FixedLossScaleManager`")
                network = _TrainOneStepWithLossScaleCell(network,
                                                         optimizer,
                                                         scale_update_cell=update_cell,
                                                         micro_batches=self._micro_batches,
                                                         l2_norm_clip=self._norm_clip,
                                                         mech=self._dp_mech).set_train()
                return network
        network = _TrainOneStepCell(network,
                                    optimizer,
                                    loss_scale,
                                    micro_batches=self._micro_batches,
                                    l2_norm_clip=self._norm_clip,
                                    mech=self._dp_mech).set_train()
        return network

    def _build_train_network(self):
        """Build train network"""
        network = self._network
        if self._micro_batches:
            if self._optimizer:
                if self._loss_scale_manager_set:
                    network = self.amp_build_train_network(network,
                                                           self._optimizer,
                                                           self._loss_fn,
                                                           level=self._amp_level,
                                                           loss_scale_manager=self._loss_scale_manager,
                                                           keep_batchnorm_fp32=self._keep_bn_fp32)
                else:
                    network = self.amp_build_train_network(network,
                                                           self._optimizer,
                                                           self._loss_fn,
                                                           level=self._amp_level,
                                                           keep_batchnorm_fp32=self._keep_bn_fp32)
            elif self._loss_fn:
                network = nn.WithLossCell(network, self._loss_fn)
        else:
            if self._optimizer:
                if self._loss_scale_manager_set:
                    network = amp.build_train_network(network,
                                                      self._optimizer,
                                                      self._loss_fn,
                                                      level=self._amp_level,
                                                      loss_scale_manager=self._loss_scale_manager,
                                                      keep_batchnorm_fp32=self._keep_bn_fp32)
                else:
                    network = amp.build_train_network(network,
                                                      self._optimizer,
                                                      self._loss_fn,
                                                      level=self._amp_level,
                                                      keep_batchnorm_fp32=self._keep_bn_fp32)
            elif self._loss_fn:
                network = nn.WithLossCell(network, self._loss_fn)

        if self._parallel_mode in (ParallelMode.SEMI_AUTO_PARALLEL, ParallelMode.AUTO_PARALLEL):
            network.set_auto_parallel()
        return network


class _ClipGradients(nn.Cell):
    """
    Clip gradients.

    Inputs:
        grads (tuple[Tensor]): Gradients.
        clip_type (int): The way to clip, 0 for 'value', 1 for 'norm'.
        clip_value (float): Specifies how much to clip.

    Outputs:
        tuple[Tensor], clipped gradients.
    """
    def __init__(self):
        super(_ClipGradients, self).__init__()
        self.clip_by_norm = nn.ClipByNorm()
        self.dtype = P.DType()

    def construct(self, grads, clip_type, clip_value):
        """
        construct a compute flow.
        """
        if clip_type not in (0, 1):
            return grads

        new_grads = ()
        for grad in grads:
            if clip_type == 0:
                t = C.clip_by_value(grad, F.tuple_to_array((-clip_value,)),
                                    F.tuple_to_array((clip_value,)))
            else:
                t = self.clip_by_norm(grad, F.tuple_to_array((clip_value,)))
            new_grads = new_grads + (t,)

        return new_grads


class _TrainOneStepWithLossScaleCell(Cell):
    r"""
    Network training with loss scaling.

    This is a training step with loss scaling. It takes a network, an optimizer and possibly a scale update
    Cell as args. The loss scale value can be updated in both host side or device side. The
    TrainOneStepWithLossScaleCell will be compiled to be graph which takes `data`, `label`, `sens` as input
    data. The `sens` is acting as loss scaling value. If you want to update it on host side, the value should
    be provided. If `sens` is not given, the loss scale update logic should be provied by `scale_update_cell`.
    If `scale_update_cell` is not None and `sens` is provided, the `scale_update_cell` will be ignored.

    Args:
        network (Cell): The training network.
        optimizer (Cell): Optimizer for updating the weights.
        scale_update_cell(Cell): The loss scaling update logic cell. Default: None.
        micro_batches (int): The number of small batches split from an origianl batch. Default: None.
        l2_norm_clip (float): Use to clip the bound, if set 1, will retun the original data. Default: None.
        mech (Mechanisms): The object can generate the different type of noise. Default: None.

    Inputs:
        - **inputs** (Tensor) - Tensor of shape :math:`(N, \ldots)`.
        - **label** (Tensor) - Tensor of shape :math:`(N, \ldots)`.
        - **scaling_sens** (Tensor) - Tensor of shape :math:`()`.

    Outputs:
        Tuple of 3 Tensor, the loss, overflow flag and current loss scaling value.

        - **loss** (Tensor) -  Tensor with shape :math:`()`.
        - **overflow** (Tensor) -  Tensor with shape :math:`()`, type is bool.
        - **loss_scale** (Tensor) -  Tensor with shape :math:`()`.

    Examples:
        >>> net_with_loss = Net()
        >>> optimizer = nn.Momentum(net_with_loss.trainable_params(), learning_rate=0.1, momentum=0.9)
        >>> manager = nn.DynamicLossScaleUpdateCell(loss_scale_value=2**12, scale_factor=2, scale_window=1000)
        >>> train_network = nn.TrainOneStepWithLossScaleCell(net_with_loss, optimizer, scale_update_cell=manager)
        >>> train_network.set_train()
        >>>
        >>> inputs = Tensor(np.ones([16, 16]).astype(np.float32))
        >>> label = Tensor(np.zeros([16, 16]).astype(np.float32))
        >>> scaling_sens = Tensor(np.full((1), np.finfo(np.float32).max), dtype=mindspore.float32)
        >>> output = train_network(inputs, label, scaling_sens)
    """

    def __init__(self, network, optimizer, scale_update_cell=None, micro_batches=None, l2_norm_clip=None, mech=None):
        super(_TrainOneStepWithLossScaleCell, self).__init__(auto_prefix=False)
        self.network = network
        self.network.add_flags(defer_inline=True)
        self.weights = ParameterTuple(network.trainable_params())
        self.optimizer = optimizer
        self.grad = C.GradOperation('grad', get_by_list=True, sens_param=True)
        self.hyper_map = C.HyperMap()
        if context.get_context("device_target") == "GPU":
            self.gpu_target = True
            self.float_status = P.FloatStatus()
            self.addn = P.AddN()
            self.reshape = P.Reshape()
        else:
            self.gpu_target = False
            self.alloc_status = NPUAllocFloatStatus()
            self.get_status = NPUGetFloatStatus()
            self.clear_status = NPUClearFloatStatus()
        self.reduce_sum = ReduceSum(keep_dims=False)
        self.base = Tensor(1, mstype.float32)
        self.less_equal = LessEqual()
        self.depend_parameter_use = ControlDepend(depend_mode=1)
        self.allreduce = P.AllReduce()
        self.parallel_mode = _get_parallel_mode()
        self.grad_reducer = F.identity
        self.reducer_flag = self.parallel_mode in [ParallelMode.DATA_PARALLEL, ParallelMode.HYBRID_PARALLEL]
        if self.reducer_flag:
            mean = _get_mirror_mean()
            degree = _get_device_num()
            self.grad_reducer = DistributedGradReducer(optimizer.parameters, mean, degree)
        self.is_distributed = self.parallel_mode != ParallelMode.STAND_ALONE

        self.loss_scale = None
        self.loss_scaling_manager = scale_update_cell
        if scale_update_cell:
            self.loss_scale = Parameter(Tensor(scale_update_cell.get_loss_scale(), dtype=mstype.float32),
                                        name="loss_scale")
        self.add_flags(has_effect=True)

        # dp params
        self._micro_batches = micro_batches
        self._l2_norm = l2_norm_clip
        self._split = P.Split(0, self._micro_batches)
        self._clip_by_global_norm = _ClipGradients()
        self._mech = mech

    def construct(self, data, label, sens=None):
        """
        construct a compute flow.
        """
        init = False
        if not self.gpu_target:
            # init overflow buffer
            init = self.alloc_status()
            # clear overflow buffer
            self.clear_status(init)

        if sens is None:
            scaling_sens = self.loss_scale
        else:
            scaling_sens = sens

        # DP clip
        weights = self.weights
        record_datas = self._split(data)
        record_labels = self._split(label)
        grads = ()
        # first index
        loss = self.network(record_datas[0], record_labels[0])
        scaling_sens_filled = C.ones_like(loss)*F.cast(scaling_sens, F.dtype(loss))
        record_grad = self.grad(self.network, weights)(record_datas[0], record_labels[0], scaling_sens_filled)
        record_grad = self._clip_by_global_norm(record_grad, GRADIENT_CLIP_TYPE, self._l2_norm)

        grad_sum = list(record_grad)
        grad_len = len(record_grad)
        for i in range(grad_len):
            grad_sum[i] = grad_sum[i].asnumpy()

        for i in range(1, self._micro_batches):
            loss = self.network(record_datas[i], record_labels[i])
            scaling_sens_filled = C.ones_like(loss)*F.cast(scaling_sens, F.dtype(loss))
            record_grad = self.grad(self.network, weights)(record_datas[i], record_labels[i], scaling_sens_filled)
            record_grad = self._clip_by_global_norm(record_grad, GRADIENT_CLIP_TYPE, self._l2_norm)
            for j in range(grad_len):
                grad_sum[j] = grad_sum[j] + record_grad[j].asnumpy()

        for i in range(grad_len):
            grad_sum[i] = Tensor(grad_sum[i], ms.float32)
        grads = tuple(grad_sum)
        loss = self.network(data, label)

        grads = self.hyper_map(F.partial(grad_scale, scaling_sens), grads)
        # apply grad reducer on grads
        grads = self.grad_reducer(grads)
        # get the overflow buffer
        if not self.gpu_target:
            self.get_status(init)
            # sum overflow buffer elements, 0:not overflow , >0:overflow
            flag_sum = self.reduce_sum(init, (0,))
        else:
            flag_sum = self.hyper_map(F.partial(_grad_overflow), grads)
            flag_sum = self.addn(flag_sum)
            # convert flag_sum to scalar
            flag_sum = self.reshape(flag_sum, (()))
        if self.is_distributed:
            # sum overflow flag over devices
            flag_reduce = self.allreduce(flag_sum)
            cond = self.less_equal(self.base, flag_reduce)
        else:
            cond = self.less_equal(self.base, flag_sum)
        overflow = cond
        if sens is None:
            overflow = self.loss_scaling_manager(self.loss_scale, cond)
        # if there is no overflow, do optimize
        if overflow:
            opt = False
        else:
            opt = self.optimizer(grads)
        ret = (loss, cond, scaling_sens)
        return F.depend(ret, opt)


class _TrainOneStepCell(Cell):
    r"""
    Network training package class.

    Wraps the network with an optimizer. The resulting Cell be trained with input data and label.
    Backward graph will be created in the construct function to do parameter updating. Different
    parallel modes are available to run the training.

    Args:
        network (Cell): The training network.
        optimizer (Cell): Optimizer for updating the weights.
        sens (Number): The scaling number to be filled as the input of backpropagation. Default value is 1.0.
        micro_batches (int): The number of small batches split from an origianl batch. Default: None.
        l2_norm_clip (float): Use to clip the bound, if set 1, will retun the original data. Default: None.
        mech (Mechanisms): The object can generate the different type of noise. Default: None.

    Inputs:
        - **data** (Tensor) - Tensor of shape :math:`(N, \ldots)`.
        - **label** (Tensor) - Tensor of shape :math:`(N, \ldots)`.

    Outputs:
        Tensor, a scalar Tensor with shape :math:`()`.

    Examples:
        >>> net = Net()
        >>> loss_fn = nn.SoftmaxCrossEntropyWithLogits()
        >>> optim = nn.Momentum(net.trainable_params(), learning_rate=0.1, momentum=0.9)
        >>> loss_net = nn.WithLossCell(net, loss_fn)
        >>> train_net = nn.TrainOneStepCell(loss_net, optim)
    """

    def __init__(self, network, optimizer, sens=1.0, micro_batches=None, l2_norm_clip=None, mech=None):
        super(_TrainOneStepCell, self).__init__(auto_prefix=False)
        self.network = network
        self.network.add_flags(defer_inline=True)
        self.weights = optimizer.parameters
        self.optimizer = optimizer
        self.grad = C.GradOperation('grad', get_by_list=True, sens_param=True)
        self.sens = sens
        self.reducer_flag = False
        self.grad_reducer = None
        parallel_mode = _get_parallel_mode()
        if parallel_mode in (ParallelMode.DATA_PARALLEL, ParallelMode.HYBRID_PARALLEL):
            self.reducer_flag = True
        if self.reducer_flag:
            mean = _get_mirror_mean()
            degree = _get_device_num()
            self.grad_reducer = DistributedGradReducer(optimizer.parameters, mean, degree)

        # dp params
        self._micro_batches = micro_batches
        self._l2_norm = l2_norm_clip
        self._split = P.Split(0, self._micro_batches)
        self._clip_by_global_norm = _ClipGradients()
        self._mech = mech

    def construct(self, data, label):
        """
        construct a compute flow.
        """
        weights = self.weights
        record_datas = self._split(data)
        record_labels = self._split(label)
        loss = self.network(record_datas[0], record_labels[0])
        sens = P.Fill()(P.DType()(loss), P.Shape()(loss), self.sens)
        record_grad = self.grad(self.network, weights)(record_datas[0], record_labels[0], sens)
        record_grad = self._clip_by_global_norm(record_grad, GRADIENT_CLIP_TYPE, self._l2_norm)
        grad_sum = list(record_grad)
        grad_len = len(record_grad)
        for i in range(grad_len):
            grad_sum[i] = grad_sum[i].asnumpy()

        for i in range(1, self._micro_batches):
            loss = self.network(record_datas[i], record_labels[i])
            sens = P.Fill()(P.DType()(loss), P.Shape()(loss), self.sens)
            record_grad = self.grad(self.network, weights)(record_datas[i], record_labels[i], sens)
            record_grad = self._clip_by_global_norm(record_grad, GRADIENT_CLIP_TYPE, self._l2_norm)
            for j in range(grad_len):
                grad_sum[j] = grad_sum[j] + record_grad[j].asnumpy()

        for i in range(grad_len):
            grad_sum[i] = Tensor(grad_sum[i], ms.float32)
        grads = tuple(grad_sum)
        loss = self.network(data, label)

        if self.reducer_flag:
            # apply grad reducer on grads
            grads = self.grad_reducer(grads)
        return F.depend(loss, self.optimizer(grads))
