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
"""Bert Trainer"""
from transformer.models.bert import BertConfig, BertNetworkWithLoss
from transformer.trainer import Trainer, TrainingConfig, parse_config
from transformer.data import create_bert_dataset


class BertTrainingConfig(TrainingConfig):
    """
    BertTrainingConfig
    """

    def __init__(self, *args, **kwargs):
        super(BertTrainingConfig, self).__init__(*args, **kwargs)
        self.epoch_size = 1
        self.data_url = ""
        self.optimizer = "adam"
        self.parallel_mode = "stand_alone"
        self.full_batch = False
        self.global_batch_size = 64
        self.ckpt_prefix = "bert"
        self.device_target = "GPU"


class BertTrainer(Trainer):
    """
    BertTrainer
    """

    def build_model_config(self):
        model_config = BertConfig()
        return model_config

    def build_model(self, model_config):
        net_with_loss = BertNetworkWithLoss(model_config, True)
        return net_with_loss

    def build_dataset(self, training_config, device_num, rank):
        return create_bert_dataset(device_num, rank, data_dir=training_config.data_path,
                                   batch_size=training_config.global_batch_size)


if __name__ == "__main__":
    config = BertTrainingConfig()
    parse_config(config)
    trainer = BertTrainer(config)
    trainer.train()
