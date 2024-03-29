# Copyright 2022 Huawei Technologies Co., Ltd
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
# ============================================================================
"""training script"""

import os
import time
import numpy as np

from mindspore import context
from mindspore import Tensor
from mindspore.train.model import Model, ParallelMode
from mindspore.train.callback import ModelCheckpoint, CheckpointConfig
from mindspore.train.loss_scale_manager import DynamicLossScaleManager
from mindspore.communication import init, get_rank
from mindspore.profiler.profiling import Profiler
from mindspore.train.serialization import load_checkpoint
from mindspore.nn.transformer import TransformerOpParallelConfig
import mindspore.dataset as ds

from transformer.models.vit import get_network, get_loss
from transformer.data.image_dataset import get_dataset
from transformer.optim.optimizer import build_optimizer
from transformer.learning_rate import get_lr
from transformer.callback import StateMonitor
from transformer.logger import get_logger
from transformer.configs.vit.config import config
from transformer.utils import print_model_size
from tasks.vision.eval_engine import get_eval_engine

if config.device_target == "Ascend":
    try:
        os.environ['MINDSPORE_HCCL_CONFIG_PATH'] = os.getenv('RANK_TABLE_FILE')

        device_id = int(os.getenv('DEVICE_ID'))   # 0 ~ 7
        local_rank = int(os.getenv('RANK_ID'))    # local_rank
        device_num = int(os.getenv('RANK_SIZE'))  # world_size
        print("distribute training")
    except TypeError:
        device_id = 0   # 0 ~ 7
        local_rank = 0    # local_rank
        device_num = 1  # world_size
        print("standalone training")
elif config.device_target == "GPU":
    device_id = 0   # 0 ~ 7
    local_rank = 0    # local_rank
    device_num = int(os.getenv('DEVICE_NUM'))  # world_size
    if device_num > 1:
        print("distribute training")
    else:
        print("standalone training")
else:
    raise ValueError(f"invalid device_targe: {config.device_target}")


def add_static_args(args):
    """add_static_args"""
    args.weight_decay = float(args.weight_decay)

    args.eval_engine = 'imagenet'
    args.split_point = 0.4
    args.poly_power = 2
    args.aux_factor = 0.4
    args.seed = 1

    if args.eval_offset < 0:
        args.eval_offset = args.max_epoch % args.eval_interval

    args.device_id = device_id
    args.local_rank = local_rank
    args.device_num = device_num
    args.dataset_name = 'imagenet'

    return args


def try_split(args, parameter_len, parameters):
    """try split parameters"""
    if args.split_point > 0:
        print("split_point={}".format(args.split_point))
        split_parameter_index = [int(args.split_point * parameter_len),]
        parameter_indices = 1
        for i in range(parameter_len):
            if i in split_parameter_index:
                parameter_indices += 1
            parameters[i].comm_fusion = parameter_indices
    else:
        print("warning!!!, no split point")


def set_running_context(args):
    """set context for runtime"""
    context.set_context(device_id=device_id,
                        mode=context.GRAPH_MODE,
                        device_target=config.device_target,
                        save_graphs=False)

    if args.device_num > 1:
        context.set_auto_parallel_context(device_num=device_num,
                                          parallel_mode=ParallelMode.SEMI_AUTO_PARALLEL,
                                          gradients_mean=True)

    # init graph kernel for gpu
    if config.device_target == "GPU":
        context.set_context(enable_graph_kernel=True,
                            graph_kernel_flags="--disable_cluster_ops=ReduceMax "
                                               "--disable_expand_ops=SoftmaxCrossEntropyWithLogits,Softmax,LogSoftmax")

    # init the distribute env
    if args.device_num > 1:
        init()


def set_parallel_config(args):
    """parallel configuration"""
    data_parallel = 8
    model_parallel = 1
    expert_parallel = 1
    pipeline_stage = 1
    micro_batch_num = 1  # micro size for pipeline training
    grad_agg_group = 4  # The fusion group size
    parallel_config = TransformerOpParallelConfig(data_parallel=data_parallel, model_parallel=model_parallel,
                                                  expert_parallel=expert_parallel, pipeline_stage=pipeline_stage,
                                                  micro_batch_num=micro_batch_num, recompute=False,
                                                  optimizer_shard=False, gradient_aggregation_group=grad_agg_group)
    args.parallel_config = parallel_config


def train_net():
    """train_net"""
    args = add_static_args(config)
    np.random.seed(args.seed)
    args.logger = get_logger(args.save_checkpoint_path, rank=local_rank)

    if args.open_profiler:
        profiler = Profiler(output_path="data_{}".format(local_rank))

    set_running_context(args)
    set_parallel_config(args)

    # network
    net = get_network(backbone_name=args.backbone, args=args)

    # set grad allreduce split point
    parameters = [param for param in net.trainable_params()]
    print_model_size(net)

    parameter_len = len(parameters)
    try_split(args, parameter_len, parameters)

    if os.path.isfile(args.pretrained):
        load_checkpoint(args.pretrained, net, strict_load=False)

    # loss
    if not args.use_label_smooth:
        args.label_smooth_factor = 0.0
    loss = get_loss(loss_name=args.loss_name, args=args)

    # train dataset
    epoch_size = args.max_epoch
    dataset = get_dataset(dataset_name=args.dataset_name,
                          do_train=True,
                          dataset_path=args.dataset_path,
                          args=args)
    ds.config.set_seed(args.seed)
    step_size = dataset.get_dataset_size()
    args.steps_per_epoch = step_size

    # evaluation dataset
    eval_dataset = get_dataset(dataset_name=args.dataset_name,
                               do_train=False,
                               dataset_path=args.eval_path,
                               args=args)

    # evaluation engine
    if args.open_profiler or eval_dataset is None or args.device_num == 1:
        args.eval_engine = ''
    eval_engine = get_eval_engine(args.eval_engine, net, eval_dataset, args)

    # loss scale
    loss_scale = DynamicLossScaleManager(init_loss_scale=65536, scale_factor=2, scale_window=2000)

    # learning rate
    lr_array = get_lr(global_step=0, lr_init=args.lr_init, lr_end=args.lr_min, lr_max=args.lr_max,
                      warmup_epochs=args.warmup_epochs, total_epochs=epoch_size, steps_per_epoch=step_size,
                      lr_decay_mode=args.lr_decay_mode, poly_power=args.poly_power)
    lr = Tensor(lr_array)

    # optimizer, group_params used in grad freeze
    opt = build_optimizer(optimizer_name=args.opt, net=net, lr=lr, args=args)

    # model
    model = Model(net, loss_fn=loss, optimizer=opt,
                  metrics=eval_engine.metric, eval_network=eval_engine.eval_network,
                  loss_scale_manager=loss_scale, amp_level="O0")
    eval_engine.set_model(model)
    args.logger.save_args(args)

    t0 = time.time()
    # equal to model._init(dataset, sink_size=step_size)
    eval_engine.compile(sink_size=step_size)

    t1 = time.time()
    args.logger.info('compile time used={:.2f}s'.format(t1 - t0))

    # callbacks
    state_cb = StateMonitor(data_size=step_size,
                            tot_batch_size=args.batch_size * device_num,
                            lrs=lr_array,
                            eval_interval=args.eval_interval,
                            eval_offset=args.eval_offset,
                            eval_engine=eval_engine,
                            logger=args.logger.info)

    cb = [state_cb,]
    if args.save_checkpoint and local_rank == 0:
        config_ck = CheckpointConfig(save_checkpoint_steps=args.save_checkpoint_epochs*step_size,
                                     keep_checkpoint_max=args.keep_checkpoint_max,
                                     async_save=True)
        ckpt_cb = ModelCheckpoint(prefix=args.backbone, directory=args.save_checkpoint_path + str(get_rank()),
                                  config=config_ck)
        cb += [ckpt_cb]

    t0 = time.time()
    model.train(epoch_size, dataset, callbacks=cb, sink_size=step_size)
    t1 = time.time()
    args.logger.info('training time used={:.2f}s'.format(t1 - t0))
    last_metric = 'last_metric[{}]'.format(state_cb.best_acc)
    args.logger.info(last_metric)

    if args.open_profiler:
        profiler.analyse()


if __name__ == '__main__':
    train_net()
