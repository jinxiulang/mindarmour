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
python lenet5_dp_model_train.py --data_path /YourDataPath --micro_batches=2
"""
import os
import argparse

import mindspore.nn as nn
from mindspore import context
from mindspore.train.callback import ModelCheckpoint
from mindspore.train.callback import CheckpointConfig
from mindspore.train.callback import LossMonitor
from mindspore.nn.metrics import Accuracy
from mindspore.train.serialization import load_checkpoint, load_param_into_net
import mindspore.dataset as ds
import mindspore.dataset.transforms.vision.c_transforms as CV
import mindspore.dataset.transforms.c_transforms as C
from mindspore.dataset.transforms.vision import Inter
import mindspore.common.dtype as mstype

from mindarmour.diff_privacy import DPModel
from mindarmour.diff_privacy import DPOptimizerClassFactory
from mindarmour.diff_privacy import PrivacyMonitorFactory
from mindarmour.utils.logger import LogUtil
from lenet5_net import LeNet5
from lenet5_config import mnist_cfg as cfg

LOGGER = LogUtil.get_instance()
TAG = 'Lenet5_train'


def generate_mnist_dataset(data_path, batch_size=32, repeat_size=1,
                           num_parallel_workers=1, sparse=True):
    """
    create dataset for training or testing
    """
    # define dataset
    ds1 = ds.MnistDataset(data_path)

    # define operation parameters
    resize_height, resize_width = 32, 32
    rescale = 1.0 / 255.0
    shift = 0.0

    # define map operations
    resize_op = CV.Resize((resize_height, resize_width),
                          interpolation=Inter.LINEAR)
    rescale_op = CV.Rescale(rescale, shift)
    hwc2chw_op = CV.HWC2CHW()
    type_cast_op = C.TypeCast(mstype.int32)

    # apply map operations on images
    if not sparse:
        one_hot_enco = C.OneHot(10)
        ds1 = ds1.map(input_columns="label", operations=one_hot_enco,
                      num_parallel_workers=num_parallel_workers)
        type_cast_op = C.TypeCast(mstype.float32)
    ds1 = ds1.map(input_columns="label", operations=type_cast_op,
                  num_parallel_workers=num_parallel_workers)
    ds1 = ds1.map(input_columns="image", operations=resize_op,
                  num_parallel_workers=num_parallel_workers)
    ds1 = ds1.map(input_columns="image", operations=rescale_op,
                  num_parallel_workers=num_parallel_workers)
    ds1 = ds1.map(input_columns="image", operations=hwc2chw_op,
                  num_parallel_workers=num_parallel_workers)

    # apply DatasetOps
    buffer_size = 10000
    ds1 = ds1.shuffle(buffer_size=buffer_size)
    ds1 = ds1.batch(batch_size, drop_remainder=True)
    ds1 = ds1.repeat(repeat_size)

    return ds1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='MindSpore MNIST Example')
    parser.add_argument('--device_target', type=str, default="Ascend", choices=['Ascend', 'GPU', 'CPU'],
                        help='device where the code will be implemented (default: Ascend)')
    parser.add_argument('--data_path', type=str, default="./MNIST_unzip",
                        help='path where the dataset is saved')
    parser.add_argument('--dataset_sink_mode', type=bool, default=False, help='dataset_sink_mode is False or True')
    parser.add_argument('--micro_batches', type=float, default=None,
                        help='optional, if use differential privacy, need to set micro_batches')
    parser.add_argument('--l2_norm_bound', type=float, default=1,
                        help='optional, if use differential privacy, need to set l2_norm_bound')
    parser.add_argument('--initial_noise_multiplier', type=float, default=0.001,
                        help='optional, if use differential privacy, need to set initial_noise_multiplier')
    args = parser.parse_args()

    context.set_context(mode=context.PYNATIVE_MODE, device_target=args.device_target, enable_mem_reuse=False)

    network = LeNet5()
    net_loss = nn.SoftmaxCrossEntropyWithLogits(is_grad=False, sparse=True, reduction="mean")
    config_ck = CheckpointConfig(save_checkpoint_steps=cfg.save_checkpoint_steps,
                                 keep_checkpoint_max=cfg.keep_checkpoint_max)
    ckpoint_cb = ModelCheckpoint(prefix="checkpoint_lenet",
                                 directory='./trained_ckpt_file/',
                                 config=config_ck)

    ds_train = generate_mnist_dataset(os.path.join(args.data_path, "train"),
                                      cfg.batch_size,
                                      cfg.epoch_size)

    if args.micro_batches and cfg.batch_size % args.micro_batches != 0:
        raise ValueError("Number of micro_batches should divide evenly batch_size")
    gaussian_mech = DPOptimizerClassFactory(args.micro_batches)
    gaussian_mech.set_mechanisms('Gaussian',
                                 norm_bound=args.l2_norm_bound,
                                 initial_noise_multiplier=args.initial_noise_multiplier)
    net_opt = gaussian_mech.create('SGD')(params=network.trainable_params(),
                                          learning_rate=cfg.lr,
                                          momentum=cfg.momentum)
    micro_size = int(cfg.batch_size // args.micro_batches)
    rdp_monitor = PrivacyMonitorFactory.create('rdp',
                                               num_samples=60000,
                                               batch_size=micro_size,
                                               initial_noise_multiplier=args.initial_noise_multiplier,
                                               per_print_times=10)
    model = DPModel(micro_batches=args.micro_batches,
                    norm_clip=args.l2_norm_bound,
                    dp_mech=gaussian_mech.mech,
                    network=network,
                    loss_fn=net_loss,
                    optimizer=net_opt,
                    metrics={"Accuracy": Accuracy()})

    LOGGER.info(TAG, "============== Starting Training ==============")
    model.train(cfg['epoch_size'], ds_train, callbacks=[ckpoint_cb, LossMonitor(), rdp_monitor],
                dataset_sink_mode=args.dataset_sink_mode)

    LOGGER.info(TAG, "============== Starting Testing ==============")
    ckpt_file_name = 'trained_ckpt_file/checkpoint_lenet-10_1875.ckpt'
    param_dict = load_checkpoint(ckpt_file_name)
    load_param_into_net(network, param_dict)
    ds_eval = generate_mnist_dataset(os.path.join(args.data_path, 'test'), batch_size=cfg.batch_size)
    acc = model.eval(ds_eval, dataset_sink_mode=False)
    LOGGER.info(TAG, "============== Accuracy: %s  ==============", acc)
