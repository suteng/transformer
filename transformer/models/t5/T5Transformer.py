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
"""
Note:
    Transformer Networks. This is interface that is subject to change or deletion.
"""
from __future__ import absolute_import

import math
import numpy as np

from mindspore import nn
from mindspore import context
from mindspore import log as logger
import mindspore.numpy
from mindspore.context import ParallelMode
from mindspore.common.tensor import Tensor
from mindspore.common.parameter import Parameter
import mindspore.common.dtype as mstype

from mindspore.ops import operations as P
from mindspore.ops import functional as F
from mindspore.nn.cell import Cell

from mindspore.nn.transformer.layers import _Linear, _check_input_shape, \
    _check_shape_equal, _check_past_none_input_none, _check_input_dtype
from mindspore.nn.transformer.moe import MoE, _check_moe_config
from mindspore.nn.transformer.transformer import default_transformer_config, default_moe_config, default_dpmp_config


from mindspore.parallel._utils import _get_parallel_mode, _is_sharding_propagation
from mindspore.common.initializer import TruncatedNormal, initializer

__all__ = [
    "LayerNorm",
    "T5MultiHeadAttention",
    "T5FeedFoward",
    "TransformerEncoder",
    "TransformerDecoder",
    "TransformerEncoderLayer",
    "TransformerDecoderLayer"]


class LayerNorm(Cell):
    """
        T5 layer norm cell
    """
    def __init__(self, normalized_shape, eps=1e-5, param_init_type=mstype.float32):
        super(LayerNorm, self).__init__()
        if param_init_type not in [mstype.float32, mstype.float16]:
            raise TypeError("The type of parameter 'param_init_type' should in [float32, float16], "
                            "but got the type : {}.".format(type(param_init_type)))
        self.gamma = Parameter(initializer('ones', normalized_shape, param_init_type), name="gamma",
                               parallel_optimizer=False)
        self.mean = P.ReduceMean(keep_dims=True)
        self.square = P.Square()
        self.sqrt = P.Sqrt()
        self.sub1 = P.Sub()
        self.sub2 = P.Sub()
        self.add = P.Add()
        self.eps = eps
        self.mul = P.Mul()
        self.add2 = P.Add()
        self.real_div = P.RealDiv()

    def construct(self, x):
        r"""
          x : batch x seq_length x hidden_size
        """
        variance = self.mean(self.square(x), -1)
        variance_eps = self.sqrt(self.add(variance, self.eps))
        output = self.real_div(x, variance_eps)
        output = self.mul(output, self.gamma)
        return output

    def shard(self, strategy):
        r"""
        Set the shard for the layer norm. the strategy size should be equal to the inputs.

        Note:
            It is valid only in semi auto parallel or auto parallel mode.
            In other parallel modes, strategies set here will be ignored.

        Args:
            strategy (tuple): The strategy for the dropout. Should be the same shape as the inputs.
        """
        self.mean.shard(strategy)
        self.square.shard(strategy)
        self.sqrt.shard(strategy)
        self.sub1.shard((strategy[0], strategy[0]))
        self.sub2.shard((strategy[0], strategy[0]))
        self.add.shard((strategy[0], ()))
        self.mul.shard((strategy[0], (1,)))
        self.add2.shard((strategy[0], (1,)))
        self.real_div.shard((strategy[0], strategy[0]))
        return self


class T5FeedFoward(Cell):
    """
        T5 feedfoward cell with relu as hidden act
    """
    def __init__(self, hidden_size,
                 ffn_hidden_size,
                 dropout_rate,
                 hidden_act='gelu',
                 expert_num=1,
                 expert_group_size=None,
                 param_init_type=mstype.float32,
                 parallel_config=default_dpmp_config):
        super(T5FeedFoward, self).__init__()
        mp = parallel_config.model_parallel
        if expert_num > 1:
            ep = parallel_config.expert_parallel
        else:
            ep = 1
        # ffn use less dp than other ops when use_moe, due to there are ops use dp and ep.
        dp = int(parallel_config.data_parallel / ep)
        if ffn_hidden_size % mp != 0:
            raise ValueError("For 'T5FeedFoward', the class variable 'ffn_hidden_size' must be a multiple of the"
                             "num of model parallel, but got the ffn_hidden_size is {} and the num of model "
                             "parallel is {}.".format(ffn_hidden_size, mp))
        if hidden_size % mp != 0:
            raise ValueError("For 'T5FeedFoward', the class variable 'hidden_size' must be a multiple of the num of "
                             "model parallel, but got the hidden_size is {} and the num of model parallel is {}."
                             .format(hidden_size, mp))
        if dropout_rate < 0 or dropout_rate >= 1:
            raise ValueError("For 'T5FeedFoward', the class variable 'dropout_rate' must be in the range [0, 1.0), "
                             "but got the value : {}.".format(dropout_rate))
        input_size = hidden_size
        output_size = ffn_hidden_size

        # Project to ffn_hidden_size
        self.mapping = _Linear(in_channels=input_size,
                               out_channels=output_size,
                               activation=hidden_act,
                               transpose_b=False,
                               expert_num=expert_num,
                               expert_group_size=expert_group_size,
                               outer_batch=dp,
                               param_init_type=param_init_type)
        self.mapping.has_bias = False

        if expert_num > 1:
            self.mapping.shard(strategy_matmul=((dp, ep, 1, 1), (ep, 1, mp)),
                               strategy_bias=((dp, ep, 1, mp), (1, ep, 1, mp)),
                               strategy_activation=((dp, ep, 1, mp),))
        else:
            self.mapping.shard(strategy_matmul=((dp, 1), (1, mp)),
                               strategy_bias=((dp, mp), (mp,)),
                               strategy_activation=((dp, mp),))
        # Project back to hidden_size
        self.projection = _Linear(in_channels=output_size,
                                  out_channels=input_size,
                                  transpose_b=False,
                                  expert_num=expert_num,
                                  expert_group_size=expert_group_size,
                                  outer_batch=dp,
                                  param_init_type=param_init_type)
        self.projection.has_bias = False
        if expert_num > 1:
            self.projection.shard(strategy_matmul=((dp, ep, 1, mp), (ep, mp, 1)),
                                  strategy_bias=((dp, ep, 1, 1), (1, ep, 1, 1)))
        else:
            self.projection.shard(strategy_matmul=((dp, mp), (mp, 1)),
                                  strategy_bias=((dp, 1), (1,)))
        self.projection.bias.parallel_optimizer = False
        self.dropout = nn.Dropout(1 - dropout_rate)
        self.dropout.dropout.shard(((dp, 1),))
        self.dropout_3d = nn.Dropout(1 - dropout_rate)
        self.dropout_3d.dropout.shard(((dp, 1, 1),))
        self.dropout_4d = nn.Dropout(1 - dropout_rate)
        self.dropout_4d.dropout.shard(((dp, ep, 1, 1),))
        self.cast = P.Cast()

    def construct(self, x):
        """The forward function of FFN"""
        _check_input_shape(F.shape(x), "x", self.cls_name, [2, 3])
        _check_input_dtype(F.dtype(x), "x", [mstype.float32, mstype.float16], self.cls_name)
        x = self.cast(x, mstype.float16)
        # returned shape is [bs, seq_length, ffn_hidden_size] or [bs * seq_length, ffn_hidden_size]
        hidden = self.mapping(x)
        output = self.projection(hidden)
        # returned shape is [bs, seq_length, ffn_hidden_size] or [bs * seq_length, ffn_hidden_size]
        if len(F.shape(output)) == 3:
            output = self.dropout_3d(output)
        elif len(F.shape(output)) == 2:
            output = self.dropout(output)
        else:
            output = self.dropout_4d(output)
        return output


class RelaPosMatrixGenerator(nn.Cell):
    """
        The relative position index generator. The result of the cell should be feed into the bias embedding table.
    """
    def __init__(self, max_relative_position, log_relative_distance):
        super(RelaPosMatrixGenerator, self).__init__()
        self._max_relative_position = max_relative_position
        self._min_relative_position = -max_relative_position

        self.tile = P.Tile()
        self.range_mat = P.Reshape()
        self.sub = P.Sub()
        self.expanddims = P.ExpandDims()
        self.cast = P.Cast()
        self.log_relative_distance = log_relative_distance

    def construct(self, relative_position, bidirectional=True, num_buckets=32):
        """The forward of the bias position"""
        relative_bucket = 0
        if bidirectional:
            num_buckets = num_buckets // 2
            relative_bucket = relative_bucket + (relative_position > 0).astype(mstype.int32) * num_buckets
            relative_position = P.Abs()(relative_position)
        else:
            relative_position = -P.Minimum()(relative_position, P.ZerosLike()(relative_position))

        max_exact = num_buckets // 2
        is_small = relative_position < max_exact
        relative_position_if_large = max_exact + (P.Log()(relative_position.astype(mstype.float32) / max_exact)
                                                  / self.log_relative_distance
                                                  * (num_buckets - max_exact))
        relative_position_if_large = relative_position_if_large.astype(mstype.int32)
        relative_position_if_large = P.Minimum()(relative_position_if_large,
                                                 mindspore.numpy.full_like(relative_position_if_large,
                                                                           num_buckets - 1))
        relative_bucket += mindspore.numpy.where(is_small, relative_position, relative_position_if_large)
        return relative_bucket


class RelaPosEmbeddingsGenerator(nn.Cell):
    """The relative position embedding generator."""
    def __init__(self,
                 depth,
                 max_relative_position,
                 initializer_range,
                 is_decoder):
        super(RelaPosEmbeddingsGenerator, self).__init__()
        self.depth = depth
        self.vocab_size = max_relative_position
        self.embeddings_table = Parameter(initializer(TruncatedNormal(initializer_range),
                                                      [self.vocab_size, self.depth]))
        self.reshape = P.Reshape()
        self.one_hot = nn.OneHot(depth=self.vocab_size)
        self.shape = P.Shape()
        self.gather = P.Gather()
        self.matmul = P.BatchMatMul()
        self.relative_attention_num_buckets = 32
        self.relative_attention_max_distance = 128
        self.is_decoder = is_decoder

        num_buckets = self.relative_attention_num_buckets

        max_exact = self.relative_attention_num_buckets // 2
        if not self.is_decoder:
            max_exact = max_exact // 2
            num_buckets //= 2

        self.log_relative_distance = math.log(self.relative_attention_max_distance / max_exact)
        self.relative_position_matrix = RelaPosMatrixGenerator(max_relative_position=max_relative_position,
                                                               log_relative_distance=self.log_relative_distance)

    def construct(self, query_length, key_length):
        """The forward function"""
        context_position = mindspore.numpy.arange(query_length, dtype=mstype.int32).expand_dims(-1)
        memory_position = mindspore.numpy.arange(key_length, dtype=mstype.int32).expand_dims(0)
        relative_position = memory_position - context_position
        relative_position_bucket = self.relative_position_matrix(
            relative_position,
            bidirectional=(not self.is_decoder),
            num_buckets=self.relative_attention_num_buckets)
        embeddings = self.gather(self.embeddings_table,
                                 relative_position_bucket, 0)
        embeddings = embeddings.transpose((2, 0, 1)).expand_dims(0)
        return embeddings


class T5MultiHeadAttention(Cell):
    """
        T5 multi head attention
    """
    def __init__(self, batch_size,
                 src_seq_length,
                 tgt_seq_length,
                 hidden_size,
                 num_heads,
                 hidden_dropout_rate=0.1,
                 attention_dropout_rate=0.1,
                 compute_dtype=mstype.float16,
                 softmax_compute_type=mstype.float32,
                 param_init_type=mstype.float32,
                 use_past=False,
                 has_relative_bias=False,
                 is_decoder=False,
                 is_cross_atten=False,
                 parallel_config=default_dpmp_config):
        super(T5MultiHeadAttention, self).__init__()
        self._is_ascend = context.get_context('device_target') in ["Ascend"]
        self.is_parallel_mode = _get_parallel_mode() in (
            ParallelMode.SEMI_AUTO_PARALLEL, ParallelMode.AUTO_PARALLEL)
        self.src_seq_length = src_seq_length
        self.tgt_seq_length = tgt_seq_length
        self.hidden_size = hidden_size
        self.batch_size = batch_size
        self.has_relative_bias = has_relative_bias
        if hidden_dropout_rate < 0 or hidden_dropout_rate >= 1:
            raise ValueError("For 'T5MultiHeadAttention', the class variable 'hidden_dropout_rate' must be "
                             "in range [0, 1.0), but got the value : {}.".format(hidden_dropout_rate))
        if attention_dropout_rate < 0 or attention_dropout_rate >= 1:
            raise ValueError("For 'T5MultiHeadAttention', the class variable 'attention_dropout_rate' must be "
                             "in range [0, 1.0), but got the value : {}.".format(attention_dropout_rate))
        if hidden_size % num_heads != 0:
            raise ValueError("For 'T5MultiHeadAttention', the class variable 'hidden_size' must be a multiple "
                             "of 'num_heads', but got the hidden_size is {} and the num_heads is {}."
                             .format(hidden_size, num_heads))
        if num_heads % parallel_config.model_parallel != 0:
            raise ValueError("For 'T5MultiHeadAttention', the class variable 'num_heads' must be a multiple of "
                             "'parallel_config.model_parallel', but got the num_heads is {} "
                             "and the parallel_config.model_parallel  is {}."
                             .format(num_heads, parallel_config.model_parallel))
        if self.is_parallel_mode and batch_size % parallel_config.data_parallel != 0:
            raise ValueError("For 'T5MultiHeadAttention', the class variable 'batch_size' must be a multiple of "
                             "'parallel_config.data_parallel', but got the batch_size is {} "
                             "and the parallel_config.data_parallel is {}."
                             .format(batch_size, parallel_config.data_parallel))
        self.is_first_iteration = True
        # Output layer
        self.projection = _Linear(in_channels=hidden_size,
                                  out_channels=hidden_size,
                                  transpose_b=False,
                                  compute_dtype=compute_dtype,
                                  param_init_type=param_init_type)
        self.projection.has_bias = False
        self.projection.shard(strategy_bias=((parallel_config.data_parallel, 1), (1,)),
                              strategy_matmul=((parallel_config.data_parallel, parallel_config.model_parallel),
                                               (parallel_config.model_parallel, 1)))
        self.projection.bias.parallel_optimizer = False
        self.transpose = P.Transpose().shard(
            ((parallel_config.data_parallel, 1, parallel_config.model_parallel, 1),))
        self.merger_head_transpose = P.Transpose().shard(
            ((parallel_config.data_parallel, parallel_config.model_parallel, 1, 1),))
        self.reshape = P.Reshape()
        self.n_head = num_heads
        # embedding size per head
        self.size_per_head = hidden_size // self.n_head
        self.concat_k = P.Concat(axis=3)
        self.concat_v = P.Concat(axis=2)
        self.multiply_data = Tensor([
            -10000.0,
        ], dtype=softmax_compute_type)
        self.batch_matmul = P.BatchMatMul().shard(
            ((parallel_config.data_parallel, parallel_config.model_parallel, 1, 1),
             (parallel_config.data_parallel, parallel_config.model_parallel, 1, 1)))
        self.real_div = P.RealDiv().shard(
            ((parallel_config.data_parallel, parallel_config.model_parallel, 1, 1), ()))
        self.sub = P.Sub().shard(
            ((1,), (parallel_config.data_parallel, 1, 1, 1)))
        self.mul = P.Mul().shard(
            ((parallel_config.data_parallel, 1, 1, 1), (1,)))
        self.add = P.Add().shard(
            ((parallel_config.data_parallel, 1, 1, 1),
             (parallel_config.data_parallel, parallel_config.model_parallel, 1, 1)))
        # Normalize factor for attention, sqrt(dk) as widely used
        self.use_past = use_past
        self.dropout = nn.Dropout(1 - hidden_dropout_rate)
        self.dropout.dropout.shard(((parallel_config.data_parallel, 1),))
        self.prob_dropout = nn.Dropout(1 - attention_dropout_rate)
        self.prob_dropout.dropout.shard(
            ((parallel_config.data_parallel, parallel_config.model_parallel, 1, 1),))
        self.softmax = nn.Softmax().to_float(softmax_compute_type)
        self.softmax.softmax.shard(((parallel_config.data_parallel, parallel_config.model_parallel, 1, 1),))
        self.softmax_3d = nn.Softmax().to_float(softmax_compute_type)
        self.softmax_3d.softmax.shard(((parallel_config.data_parallel, parallel_config.model_parallel, 1),))
        self.expand_dims = P.ExpandDims().shard(((parallel_config.data_parallel, 1, 1),))

        # Query
        self.dense1 = _Linear(hidden_size,
                              hidden_size,
                              compute_dtype=compute_dtype,
                              param_init_type=param_init_type)
        self.dense1.has_bias = False
        self.dense1.shard(strategy_matmul=((parallel_config.data_parallel, 1), (parallel_config.model_parallel, 1)),
                          strategy_bias=((parallel_config.data_parallel, parallel_config.model_parallel),
                                         (parallel_config.model_parallel,)))
        # Key
        self.dense2 = _Linear(hidden_size,
                              hidden_size,
                              compute_dtype=compute_dtype,
                              param_init_type=param_init_type)
        self.dense2.has_bias = False
        self.dense2.shard(strategy_matmul=((parallel_config.data_parallel, 1), (parallel_config.model_parallel, 1)),
                          strategy_bias=((parallel_config.data_parallel, parallel_config.model_parallel),
                                         (parallel_config.model_parallel,)))

        # Value
        self.dense3 = _Linear(hidden_size,
                              hidden_size,
                              compute_dtype=compute_dtype,
                              param_init_type=param_init_type)
        self.dense3.has_bias = False
        self.dense3.shard(strategy_matmul=((parallel_config.data_parallel, 1), (parallel_config.model_parallel, 1)),
                          strategy_bias=((parallel_config.data_parallel, parallel_config.model_parallel),
                                         (parallel_config.model_parallel,)))
        self.dtype = compute_dtype
        self.softmax_dtype = softmax_compute_type

        self.is_decoder = is_decoder
        self.has_relative_bias = has_relative_bias

        self.cross_attention = is_cross_atten
        self.cross_bias = None
        if self.cross_attention:
            self.cross_bias = Parameter(initializer("zero", [1, self.src_seq_length, self.tgt_seq_length]),
                                        name='cross_attention_bias', parallel_optimizer=False)
        if self.has_relative_bias:
            self.bias_generator = RelaPosEmbeddingsGenerator(depth=num_heads,
                                                             max_relative_position=32,
                                                             initializer_range=0.02,
                                                             is_decoder=self.is_decoder)

        if self.use_past:
            # operators used for state reuse
            seq_range = np.arange(src_seq_length).reshape(1, 1, -1)
            self.range = Tensor(np.tile(seq_range, (batch_size, 1, 1)), mstype.int32)
            self.seq_length = src_seq_length
            self.attention_mask = Tensor(np.tril(np.ones(shape=(self.seq_length, self.seq_length))), mstype.int32)
            self.slice = P.StridedSlice().shard(((1, 1, 1, 1),))
            self.not_equal = P.NotEqual().shard(((1, 1, 1, 1), ()))
            self.reducesum = P.ReduceSum().shard(((1, 1, 1, 1),))
            self.expand_dims = P.ExpandDims().shard(((1, 1, 1),))
            self.tensor_le = P.LessEqual().shard(((1, 1, 1), (1, 1, 1)))
            self.add = P.Add().shard(((1, 1, 1, 1), (1, 1, 1, 1)))
            self.equal = P.Equal().shard(((1, 1, 1), (1, 1, 1)))
            self.sub1 = P.Sub().shard(((1,), ()))
            self.tile = P.Tile().shard(((1, 1, 1, 1),))
            self.less = P.Less().shard(((1, 1, 1), (1, 1, 1)))
            self.mul1 = P.Mul().shard(((1, 1, 1, 1), (1, 1, 1, 1)))

    def construct(self, query_tensor, key_tensor, value_tensor, attention_mask, bias=None, key_past=None,
                  value_past=None, batch_valid_length=None):
        """forward function for attention"""
        self._check_inputs(query_tensor, key_tensor, value_tensor, attention_mask, key_past,
                           value_past, batch_valid_length)
        query_tensor, key_tensor, value_tensor, batch_size, ori_shape = self._convert_to_2d_tensor(query_tensor,
                                                                                                   key_tensor,
                                                                                                   value_tensor,
                                                                                                   attention_mask)
        ori_dtype = F.dtype(query_tensor)
        query_tensor = F.cast(query_tensor, self.dtype)
        key_tensor = F.cast(key_tensor, self.dtype)
        value_tensor = F.cast(value_tensor, self.dtype)
        # multi head attention: query, key, value are derived from the same inputs
        query = self.dense1(query_tensor)
        key = self.dense2(key_tensor)
        value = self.dense3(value_tensor)
        # the returned shape is [bs, num_heads, seq_length, size_per_head]
        query = self.transpose(
            F.reshape(
                query,
                (batch_size, -1, self.n_head, self.size_per_head)),
            (0, 2, 1, 3))
        # the returned shape is [bs, size_per_head, seq_length, num_heads]
        key = self.transpose(
            F.reshape(
                key, (batch_size, -1, self.n_head, self.size_per_head)),
            (0, 2, 3, 1))
        # the returned shape is [bs, num_heads, seq_length, size_per_head]
        value = self.transpose(
            F.reshape(
                value,
                (batch_size, -1, self.n_head, self.size_per_head)),
            (0, 2, 1, 3))
        # support input shape is [bs, seq, seq] or [bs, heads, seq, seq]
        if len(F.shape(attention_mask)) == 3:
            # expand attention mask from [bs, seq, seq] -> [bs, 1, seq, seq]
            attention_mask = self.expand_dims(attention_mask, 1)
        # key and value for current token(s)
        key_present = key
        value_present = value
        if self.use_past:
            # The first graph with the input size of (bs, seq_length)
            if self.is_first_iteration:
                # Get the valid input length without padding
                valid_length_vector = F.cast(self.less(self.range, batch_valid_length.view(-1, 1, 1)), self.dtype)
                # Cover the key and value numbers corresponding to the padding position
                key_present = self.mul1(key, self.expand_dims(valid_length_vector, 2))
                value_present = self.mul1(value, self.expand_dims(valid_length_vector, 3))
            # The second graph with the inpus size of (bs, 1)
            # the shape of query is (bs, num_heads, 1, size_per_head)
            # the shape of key is   (bs, num_heads, size_per_head, 1)
            # the shape of value is (bs, num_heads, 1, size_per_head)
            else:
                # Get the current token position index
                valid_length = self.reducesum(F.cast(self.not_equal(self.slice(key_past, (0, 0, 0, 0),
                                                                               (F.shape(key_tensor)[0], 1, 1,
                                                                                self.src_seq_length),
                                                                               (1, 1, 1, 1)),
                                                                    0), mstype.float32), (1, 2, 3))
                valid_length = F.reshape(valid_length, (-1, 1, 1))
                valid_length_vector = F.cast(self.equal(valid_length, self.range), self.dtype)
                # Pad the key and value to seq_length with only the position index not zero
                current_key = self.mul1(self.tile(key, (1, 1, 1, self.seq_length)),
                                        self.expand_dims(valid_length_vector, 2))
                current_value = self.mul1(self.tile(value, (1, 1, self.seq_length, 1)),
                                          self.expand_dims(valid_length_vector, 3))
                # Concat the previous saved state and current state
                key = self.add(key_past, current_key)
                value = self.add(value_past, current_value)
                # Update key_present and value_present for state update
                key_present = key
                value_present = value
                attention_mask = F.reshape(self.attention_mask, (self.seq_length, self.seq_length, 1, 1))

        layer_present = (key_present, value_present)
        # multi head attention considering attention mask
        # the return shape is [bs * seq_length, hidden_size]
        attention, bias = self._attn(query, key, value, attention_mask, bias)
        # Output
        output = self.projection(attention)
        output = self.dropout(output)
        output = F.reshape(output, ori_shape)
        output = F.cast(output, ori_dtype)
        return output, layer_present, bias

    def _check_inputs(self, query_tensor, key_tensor, value_tensor, attention_mask, key_past=None,
                      value_past=None, batch_valid_length=None):
        r"""Check inputs"""
        if not self.use_past or (self.use_past and self.is_first_iteration):
            _check_shape_equal(F.shape(query_tensor), "query_tensor", self.cls_name,
                               [[self.batch_size, self.src_seq_length, self.hidden_size],
                                [self.batch_size * self.src_seq_length, self.hidden_size]])
            _check_shape_equal(F.shape(key_tensor), "key_tensor", self.cls_name,
                               [[self.batch_size, self.tgt_seq_length, self.hidden_size],
                                [self.batch_size * self.tgt_seq_length, self.hidden_size]])
            _check_shape_equal(F.shape(value_tensor), "value_tensor", self.cls_name,
                               [[self.batch_size, self.tgt_seq_length, self.hidden_size],
                                [self.batch_size * self.tgt_seq_length, self.hidden_size]])
            _check_shape_equal(F.shape(attention_mask), "attention_mask", self.cls_name,
                               [self.batch_size, self.src_seq_length, self.tgt_seq_length])
        else:
            _check_shape_equal(F.shape(query_tensor), "query_tensor", self.cls_name,
                               [[self.batch_size, 1, self.hidden_size], [self.batch_size, self.hidden_size]])
            _check_shape_equal(F.shape(key_tensor), "key_tensor", self.cls_name,
                               [[self.batch_size, 1, self.hidden_size], [self.batch_size, self.hidden_size]])
            _check_shape_equal(F.shape(value_tensor), "value_tensor", self.cls_name,
                               [[self.batch_size, 1, self.hidden_size], [self.batch_size, self.hidden_size]])
            _check_shape_equal(F.shape(attention_mask), "attention_mask", self.cls_name,
                               [[self.batch_size, 1, self.tgt_seq_length], [self.batch_size, self.hidden_size]])

        _check_input_dtype(F.dtype(query_tensor), "query_tensor", [mstype.float32, mstype.float16], self.cls_name)
        _check_input_dtype(F.dtype(key_tensor), "key_tensor", [mstype.float32, mstype.float16], self.cls_name)
        _check_input_dtype(F.dtype(value_tensor), "value_tensor", [mstype.float32, mstype.float16], self.cls_name)
        _check_input_dtype(F.dtype(attention_mask), "attention_mask", [mstype.float32, mstype.float16], self.cls_name)

        key_is_tensor = isinstance(key_past, Tensor)
        value_is_tensor = isinstance(value_past, Tensor)
        batch_valid_length_is_tensor = isinstance(batch_valid_length, Tensor)
        key_is_default = key_past is None
        value_is_default = value_past is None
        batch_is_default = batch_valid_length is None
        _check_past_none_input_none(self.use_past, "key_past", self.cls_name, None, key_is_tensor,
                                    key_is_default)
        _check_past_none_input_none(self.use_past, "value_past", self.cls_name, None, value_is_tensor,
                                    value_is_default)
        _check_past_none_input_none(self.use_past, "batch_valid_length", self.cls_name, None,
                                    batch_valid_length_is_tensor, batch_is_default)
        if self.use_past:
            _check_shape_equal(F.shape(key_past), "key_past", self.cls_name,
                               [self.batch_size, self.n_head, self.size_per_head, self.tgt_seq_length])
            _check_input_dtype(F.dtype(key_past), "key_past", [mstype.float16], self.cls_name)
            _check_shape_equal(F.shape(value_past), "value_past", self.cls_name,
                               [self.batch_size, self.n_head, self.tgt_seq_length, self.size_per_head])
            _check_input_dtype(F.dtype(value_past), "value_past", [mstype.float16], self.cls_name)
            _check_shape_equal(F.shape(batch_valid_length), "batch_valid_length", self.cls_name, [self.batch_size])
            _check_input_dtype(F.dtype(batch_valid_length), "batch_valid_length", [mstype.int32], self.cls_name)
        return True

    def _convert_to_2d_tensor(self, query_tensor, key_tensor, value_tensor, attention_mask):
        """convert a nd tensor to a 2d tensor"""
        query_shape = F.shape(query_tensor)
        query_tensor = F.reshape(query_tensor, (-1, query_shape[-1]))
        key_shape = F.shape(key_tensor)
        key_tensor = F.reshape(key_tensor, (-1, key_shape[-1]))
        value_shape = F.shape(value_tensor)
        value_tensor = F.reshape(value_tensor, (-1, value_shape[-1]))
        return query_tensor, key_tensor, value_tensor, F.shape(attention_mask)[0], query_shape

    def _merge_heads(self, x):
        """
        convert a 4d input to a 2d output

        Inputs:
            x: input tensor

        Output:
            x_merge: the 2d output
        """
        x = self.merger_head_transpose(
            x, (0, 2, 1, 3))  # bs, seq_length, head, size_per_head
        x_shape = P.Shape()(x)
        new_shape = (-1, x_shape[-2] * x_shape[-1])
        x_merge = self.reshape(x, new_shape)
        return x_merge

    def _softmax(self, attention_scores):
        """
        For the consideration of the performance, do softmax according to different situations
        :param attention_scores: a 3d tensor before softmax
        :return: the attention scores.
        """

        if self._is_ascend and self.softmax_dtype == mstype.float16 or not self._is_ascend:
            attention_probs = self.softmax(attention_scores)
        else:
            shape = F.shape(attention_scores)
            # attention probs
            attention_probs = self.softmax_3d(
                F.reshape(attention_scores,
                          (shape[0], -1, shape[-1])))
            attention_probs = F.reshape(attention_probs, shape)
        return attention_probs

    def _attn(self, query, key, value, attention_mask, bias):
        """
        Get the weighted score along the seq_length

        Inputs:
            query: the query matrix
            key: the key matrix
            value: the value matrix
            attention_mask: the attention mask matrix with shape (batch_size,
            1, seq_length, seq_length)
        Outputs:
            weighted_values: Tensor, the weighted sum scores
        """
        # Normalize query and key before MatMul, default off
        # Attention score [bs, num_heads, seq_length, seq_length]
        score = self.batch_matmul(query, key)

        ori_dtype = P.DType()(score)
        score = P.Cast()(score, self.softmax_dtype)

        # for input size of (bs, 1) namely the second graph,
        # the shape of attention_mask matrix should be (bs, 1, 1, seq_length)
        if self.use_past and not self.is_first_iteration:
            # Calculate the current total token
            current_index = self.reducesum(F.cast(self.not_equal(self.slice(key, (0, 0, 0, 0),
                                                                            (F.shape(query)[0], 1, 1, self.seq_length),
                                                                            (1, 1, 1, 1)),
                                                                 0), mstype.float32), (1, 2, 3))
            # Get the precise position index
            index = self.sub1(F.cast(current_index, mstype.int32), 1)
            index = F.reshape(index, (-1, 1, 1))
            # Calculate the attention_mask matrix via the position index
            attention_mask = F.cast(self.tensor_le(self.range, index), mstype.int32)
            attention_mask = self.expand_dims(attention_mask, 2)
        if bias is None:
            if self.has_relative_bias:
                bias = self.bias_generator(self.src_seq_length, self.tgt_seq_length)
            elif self.cross_attention:
                bias = P.ExpandDims()(self.cross_bias, 0)

        score = self.add(score, bias)
        # Minus 10000 for the position where masked to exclude them from softmax
        multiplu_out = self.sub(
            P.Cast()(F.tuple_to_array((1.0,)), P.DType()(score)),
            P.Cast()(attention_mask, P.DType()(score)))

        adder = self.mul(multiplu_out, self.multiply_data)
        attention_scores = self.add(adder, score)
        # attention probs
        attention_probs = self._softmax(attention_scores)
        attention_probs = P.Cast()(attention_probs, ori_dtype)

        attention_probs = self.prob_dropout(attention_probs)
        # Weighted sum output [bs, num_heads, seq_length, size_per_head]
        weighted_values = self.batch_matmul(attention_probs, value)
        attention_merge = self._merge_heads(weighted_values)
        return attention_merge, bias


class TransformerEncoderLayer(Cell):
    """
        Transformer Encoder Layer
    """
    def __init__(self,
                 batch_size,
                 hidden_size,
                 ffn_hidden_size,
                 num_heads,
                 seq_length,
                 attention_dropout_rate=0.1,
                 hidden_dropout_rate=0.1,
                 post_layernorm_residual=False,
                 layernorm_compute_type=mstype.float32,
                 softmax_compute_type=mstype.float32,
                 param_init_type=mstype.float32,
                 hidden_act='gelu',
                 use_past=False,
                 moe_config=default_moe_config,
                 has_bias=False,
                 parallel_config=default_dpmp_config):
        super(TransformerEncoderLayer, self).__init__()
        if num_heads % parallel_config.model_parallel != 0:
            raise ValueError(
                "For 'TransformerEncoderLayer', the class variable 'num_heads' must be divisibled by the "
                "'parallel_config.model_parallel', but got the num_heads is {} and "
                "parallel_config.model_parallel is {}.".format(num_heads, parallel_config.model_parallel))
        if hidden_size % parallel_config.model_parallel != 0:
            raise ValueError(
                "For 'TransformerEncoderLayer', the class variable 'hidden_size' must be divisibled by "
                "the 'parallel_config.model_parallel', but got the hidden_size is {} and parallel_config."
                " model_parallel is {}.".format(hidden_size, parallel_config.model_parallel))
        if ffn_hidden_size % parallel_config.model_parallel != 0:
            raise ValueError(
                "For 'TransformerEncoderLayer', the class variable 'ffn_hidden_size' must be divisibled "
                "by the 'parallel_config.model_parallel', but got the ffn_hidden_size is {} "
                "and parallel_config. model_parallel is {}.".format(ffn_hidden_size,
                                                                    parallel_config.model_parallel))
        _check_moe_config(moe_config, parallel_config)
        self.use_moe = (moe_config.expert_num > 1)
        self.use_past = use_past
        self.seq_length = seq_length
        self.hidden_size = hidden_size
        self.batch_size = batch_size
        self.layernorm1 = LayerNorm((hidden_size,)).to_float(layernorm_compute_type)
        self.layernorm2 = LayerNorm((hidden_size,)).to_float(layernorm_compute_type)

        self.attention = T5MultiHeadAttention(batch_size=batch_size,
                                              src_seq_length=seq_length,
                                              tgt_seq_length=seq_length,
                                              hidden_size=hidden_size,
                                              num_heads=num_heads,
                                              hidden_dropout_rate=hidden_dropout_rate,
                                              attention_dropout_rate=attention_dropout_rate,
                                              softmax_compute_type=softmax_compute_type,
                                              param_init_type=param_init_type,
                                              use_past=use_past,
                                              is_decoder=False,
                                              has_relative_bias=has_bias,
                                              parallel_config=parallel_config.dpmp if self.use_moe
                                              else parallel_config)
        if self.use_moe:
            self.output = MoE(hidden_size=hidden_size,
                              dropout_rate=hidden_dropout_rate,
                              ffn_hidden_size=ffn_hidden_size,
                              param_init_type=param_init_type,
                              hidden_act=hidden_act,
                              moe_config=moe_config,
                              parallel_config=parallel_config)
        else:
            # Feed Forward Network, FFN
            self.output = T5FeedFoward(hidden_size=hidden_size,
                                       dropout_rate=hidden_dropout_rate,
                                       ffn_hidden_size=ffn_hidden_size,
                                       param_init_type=param_init_type,
                                       hidden_act=hidden_act,
                                       parallel_config=parallel_config)
        self.post_layernorm_residual = post_layernorm_residual
        self.add = P.Add().shard(((parallel_config.data_parallel, 1), (parallel_config.data_parallel, 1)))
        self.add_3d = P.Add().shard(((parallel_config.data_parallel, 1, 1), (parallel_config.data_parallel, 1, 1)))
        self.dtype = mstype.float16
        self.key_past = None
        self.value_past = None

        if self.use_past:
            # operator used for state reuse
            self.reducesum = P.ReduceSum().shard(((1, 1, 1, 1),))
            self.not_equal = P.NotEqual().shard(((1, 1, 1, 1), ()))
            self.slice = P.StridedSlice().shard(((1, 1, 1, 1),))
            size_per_head = int(hidden_size / num_heads)
            self.key_shape = (batch_size, num_heads, size_per_head, seq_length)
            self.value_shape = (batch_size, num_heads, seq_length, size_per_head)
            # parameters saving key and value states
            self.key_past = Parameter(Tensor(np.zeros(shape=self.key_shape), self.dtype), name="key_past")
            self.value_past = Parameter(Tensor(np.zeros(shape=self.value_shape), self.dtype), name="value_past")
            self.tile = P.Tile().shard(((1, 1),))
            self.mul = P.Mul().shard(((1, 1, 1, 1), (1,)))
            self.assign = P.Assign().shard(((1, 1, 1, 1), (1, 1, 1, 1)))

        if _get_parallel_mode() in (ParallelMode.AUTO_PARALLEL,) and _is_sharding_propagation():
            pass
        elif _get_parallel_mode() not in (ParallelMode.AUTO_PARALLEL,):
            self.layernorm1.shard(((parallel_config.data_parallel, 1),))
            self.layernorm2.shard(((parallel_config.data_parallel, 1),))
        else:
            raise RuntimeError(f"The {self.cls_name} only support sharding propagation or "
                               f"semi-auto parallel mode now.")

    def construct(self, x, input_mask, bias, init_reset=True, batch_valid_length=None):
        """Forward function of the EncoderLayer"""
        self._check_input(x, input_mask, init_reset, batch_valid_length)
        x_shape = F.shape(x)
        x = F.reshape(x, (-1, x_shape[-1]))
        input_x = self.layernorm1(x)
        input_x = F.cast(input_x, self.dtype)

        # indicate whether reset saved states
        key_reset = None
        value_reset = None

        if self.use_past:
            # reset states, init_reset True for reuse and False for reset
            key_reset = self.assign(self.key_past, self.mul(self.key_past, F.cast(init_reset, self.dtype)))
            value_reset = self.assign(self.value_past, self.mul(self.value_past, F.cast(init_reset, self.dtype)))
            # add dependency for desired execution order
            input_x = F.depend(input_x, key_reset)
            input_x = F.depend(input_x, value_reset)

        attention, layer_present, bias = self.attention(input_x, input_x, input_x, input_mask, bias,
                                                        self.key_past, self.value_past, batch_valid_length)
        # For post-layernorm the inputs for residual path are output of self-attention and output of layernorm
        if self.post_layernorm_residual:
            x = self.add(input_x, attention)
        # For pre-layernorm the inputs for residual path are output of self-attention and input of this layer
        else:
            x = self.add(x, attention)

        output_x = self.layernorm2(x)
        output_x = F.cast(output_x, self.dtype)
        aux_loss = None
        if self.use_moe:
            mlp_logit, aux_loss = self.output(output_x)
        else:
            mlp_logit = self.output(output_x)

        value_update = None
        key_update = None
        if self.use_past:
            # current key and value
            key_present, value_present = layer_present
            # update key and value calculated this step
            key_update = self.assign(self.key_past, key_present)
            value_update = self.assign(self.value_past, value_present)
            # add dependency for desired execution order
            key_update = F.depend(key_update, key_reset)
            value_update = F.depend(value_update, value_reset)

        # add dependency for desired execution order
        mlp_logit = F.depend(mlp_logit, value_update)
        mlp_logit = F.depend(mlp_logit, key_update)

        # if shape is 3d, we reshape the inputs of the add
        if len(x_shape) == 3:
            output_x = P.Reshape()(output_x, x_shape)
            mlp_logit = P.Reshape()(mlp_logit, x_shape)
            x = P.Reshape()(x, x_shape)

            if self.post_layernorm_residual:
                output = self.add_3d(output_x, mlp_logit)
            else:
                output = self.add_3d(x, mlp_logit)
        else:
            if self.post_layernorm_residual:
                output = self.add(output_x, mlp_logit)
            else:
                output = self.add(x, mlp_logit)
            output = F.reshape(output, x_shape)

        if self.use_moe:
            return output, layer_present, aux_loss
        return output, layer_present, bias

    def _check_input(self, x, input_mask, init_reset, batch_valid_length):
        r"""Check inputs"""
        if not self.use_past or (self.use_past and self.is_first_iteration):
            _check_shape_equal(F.shape(x), "x", self.cls_name,
                               [[self.batch_size, self.seq_length, self.hidden_size],
                                [self.batch_size * self.seq_length, self.hidden_size]])
            _check_shape_equal(F.shape(input_mask), "input_mask", self.cls_name,
                               [self.batch_size, self.seq_length, self.seq_length])
        else:
            _check_shape_equal(F.shape(x), "x", self.cls_name, [self.batch_size, 1, self.hidden_size])
            _check_shape_equal(F.shape(input_mask), "input_mask", self.cls_name,
                               [self.batch_size, 1, self.seq_length])
        _check_input_dtype(F.dtype(x), "x", [mstype.float32, mstype.float16], self.cls_name)
        _check_input_dtype(F.dtype(input_mask), "input_mask", [mstype.float32, mstype.float16], self.cls_name)

        init_reset_is_tensor = isinstance(init_reset, Tensor)
        init_reset_is_default = init_reset is True
        batch_valid_length_is_tensor = isinstance(batch_valid_length, Tensor)
        batch_is_default = batch_valid_length is None
        _check_past_none_input_none(self.use_past, "init_reset", self.cls_name, True, init_reset_is_tensor,
                                    init_reset_is_default)
        _check_past_none_input_none(self.use_past, "batch_valid_length", self.cls_name, None,
                                    batch_valid_length_is_tensor, batch_is_default)

        if self.use_past:
            _check_shape_equal(F.shape(init_reset), "init_reset", self.cls_name, [1])
            _check_input_dtype(F.dtype(init_reset), "init_reset", [mstype.bool_], self.cls_name)
            _check_shape_equal(F.shape(batch_valid_length), "batch_valid_length", self.cls_name, [self.batch_size])
            _check_input_dtype(F.dtype(batch_valid_length), "batch_valid_length", [mstype.int32], self.cls_name)
        return True


class TransformerDecoderLayer(Cell):
    """
        The Transformer Decoder Layer
    """
    def __init__(self, hidden_size,
                 ffn_hidden_size,
                 num_heads,
                 batch_size,
                 src_seq_length,
                 tgt_seq_length,
                 attention_dropout_rate=0.1,
                 hidden_dropout_rate=0.1,
                 post_layernorm_residual=False,
                 use_past=False,
                 layernorm_compute_type=mstype.float32,
                 softmax_compute_type=mstype.float32,
                 param_init_type=mstype.float32,
                 hidden_act='gelu',
                 has_bias=False,
                 moe_config=default_moe_config,
                 parallel_config=default_dpmp_config):
        super(TransformerDecoderLayer, self).__init__()
        if num_heads % parallel_config.model_parallel != 0:
            raise ValueError("For 'TransformerDecoderLayer', the class variable 'num_heads' must be divisibled by "
                             "'parallel_config.model_parallel', but got the num_heads is {} and "
                             "parallel_config.model_parallel is {}.".format(num_heads,
                                                                            parallel_config.model_parallel))
        if hidden_size % parallel_config.model_parallel != 0:
            raise ValueError(
                "For 'TransformerDecoderLayer', the class variable 'hidden_size' must be divisibled by "
                "'parallel_config.model_parallel', but got the hidden_size is {} and "
                "parallel_config.model_parallel is {}.".format(hidden_size, parallel_config.model_parallel))
        if ffn_hidden_size % parallel_config.model_parallel != 0:
            raise ValueError("For 'TransformerDecoderLayer', the class variable 'ffn_hidden_size' must be "
                             "divisibled by 'parallel_config.model_parallel', but got the ffn_hidden_size is {} "
                             "and parallel_config.model_parallel is {}."
                             .format(ffn_hidden_size, parallel_config.model_parallel))
        _check_moe_config(moe_config, parallel_config)
        self.use_moe = (moe_config.expert_num > 1)
        if use_past:
            raise ValueError(f"The {self.cls_name} does not support use_past=True.")
        self.batch_size = batch_size
        self.use_past = use_past
        self.softmax_compute_type = softmax_compute_type

        self.src_seq_length = src_seq_length
        self.tgt_seq_length = tgt_seq_length
        self.use_past = use_past
        self.hidden_size = hidden_size

        self.layernorm1 = LayerNorm((hidden_size,)).to_float(layernorm_compute_type)
        self.layernorm1.shard(((parallel_config.data_parallel, 1),))
        self.layernorm2 = LayerNorm((hidden_size,)).to_float(layernorm_compute_type)
        self.layernorm2.shard(((parallel_config.data_parallel, 1),))
        self.attention = T5MultiHeadAttention(hidden_size=hidden_size,
                                              num_heads=num_heads,
                                              batch_size=batch_size,
                                              src_seq_length=tgt_seq_length,
                                              tgt_seq_length=tgt_seq_length,
                                              hidden_dropout_rate=hidden_dropout_rate,
                                              attention_dropout_rate=attention_dropout_rate,
                                              use_past=use_past,
                                              softmax_compute_type=softmax_compute_type,
                                              param_init_type=param_init_type,
                                              is_decoder=True,
                                              has_relative_bias=has_bias,
                                              parallel_config=parallel_config.dpmp if self.use_moe
                                              else parallel_config)

        # Cross attention with the output of encoder as memory tensor
        self.cross_attention = T5MultiHeadAttention(hidden_size=hidden_size,
                                                    num_heads=num_heads,
                                                    batch_size=batch_size,
                                                    src_seq_length=tgt_seq_length,
                                                    tgt_seq_length=src_seq_length,
                                                    hidden_dropout_rate=hidden_dropout_rate,
                                                    attention_dropout_rate=attention_dropout_rate,
                                                    softmax_compute_type=softmax_compute_type,
                                                    use_past=use_past,
                                                    is_decoder=True,
                                                    is_cross_atten=True,
                                                    has_relative_bias=False,
                                                    param_init_type=param_init_type,
                                                    parallel_config=parallel_config.dpmp
                                                    if self.use_moe else parallel_config)
        self.cross_attention_layernorm = LayerNorm((hidden_size,)).to_float(
            layernorm_compute_type)
        self.cross_attention_layernorm.shard(((parallel_config.data_parallel, 1),))

        if self.use_moe:
            self.output = MoE(hidden_size=hidden_size,
                              dropout_rate=hidden_dropout_rate,
                              ffn_hidden_size=ffn_hidden_size,
                              param_init_type=param_init_type,
                              hidden_act=hidden_act,
                              moe_config=moe_config,
                              parallel_config=parallel_config)
        else:
            # Feed Forward Network, FFN
            self.output = T5FeedFoward(hidden_size=hidden_size,
                                       dropout_rate=hidden_dropout_rate,
                                       ffn_hidden_size=ffn_hidden_size,
                                       hidden_act=hidden_act,
                                       param_init_type=param_init_type,
                                       parallel_config=parallel_config)
        self.post_layernorm_residual = post_layernorm_residual
        self.add = P.Add().shard(((parallel_config.data_parallel, 1), (parallel_config.data_parallel, 1)))
        self.add_3d = P.Add().shard(((parallel_config.data_parallel, 1, 1), (parallel_config.data_parallel, 1, 1)))
        self.dtype = mstype.float16
        self.key_past = None
        self.value_past = None
        if self.use_past:
            # operator used for state reuse
            self.reducesum = P.ReduceSum().shard(((1, 1, 1, 1),))
            self.not_equal = P.NotEqual().shard(((1, 1, 1, 1), ()))
            self.slice = P.StridedSlice().shard(((1, 1, 1, 1),))
            size_per_head = int(hidden_size / num_heads)
            self.key_shape = (batch_size, num_heads, size_per_head, tgt_seq_length)
            self.value_shape = (batch_size, num_heads, tgt_seq_length, size_per_head)
            # parameters saving key and value states
            self.key_past = Parameter(Tensor(np.zeros(shape=self.key_shape), self.dtype), name="key_past")
            self.value_past = Parameter(Tensor(np.zeros(shape=self.value_shape), self.dtype), name="value_past")
            self.tile = P.Tile().shard(((1, 1),))
            self.mul = P.Mul().shard(((1, 1, 1, 1), (1,)))
            self.assign = P.Assign().shard(((1, 1, 1, 1), (1, 1, 1, 1)))

    def construct(self, hidden_stats,
                  decoder_mask,
                  encoder_output=None,
                  memory_mask=None,
                  self_bias=None,
                  encoder_attention_bias=None,
                  init_reset=True, batch_valid_length=None):
        """The forward function of the decoder layer"""
        self._check_input(hidden_stats, decoder_mask, encoder_output, memory_mask, init_reset, batch_valid_length)
        # the returned shape is [bs, seq_length, embedding_size] or [bs * seq_length, embedding_size]
        hidden_shape = F.shape(hidden_stats)
        hidden_stats = F.reshape(hidden_stats, (-1, hidden_shape[-1]))
        input_x = self.layernorm1(hidden_stats)
        input_x = F.cast(input_x, self.dtype)

        # indicate whether reset saved states
        key_reset = None
        value_reset = None
        if self.use_past:
            # reset states, init_reset True for reuse and False for reset
            key_reset = self.assign(self.key_past, self.mul(self.key_past, F.cast(init_reset, self.dtype)))
            value_reset = self.assign(self.value_past, self.mul(self.value_past, F.cast(init_reset, self.dtype)))
            # add dependency for desired execution order
            input_x = F.depend(input_x, key_reset)
            input_x = F.depend(input_x, value_reset)

        attention, layer_present, self_bias = self.attention(input_x, input_x, input_x, decoder_mask, self_bias,
                                                             self.key_past,
                                                             self.value_past, batch_valid_length)
        # For post-layernorm the inputs for residual path are output of self-attention and output of layernorm
        if self.post_layernorm_residual:
            x = self.add(input_x, attention)
        # For pre-layernorm the inputs for residual path are output of self-attention and input of this layer
        else:
            x = self.add(hidden_stats, attention)

        middle_output = None
        if encoder_output is not None:
            middle_output = self.cross_attention_layernorm(x)
            middle_output = F.cast(middle_output, self.dtype)
            encoder_output = F.cast(encoder_output, self.dtype)
            cross_attn_out, cross_layer_present, encoder_attention_bias = self.cross_attention(middle_output,
                                                                                               encoder_output,
                                                                                               encoder_output,
                                                                                               memory_mask,
                                                                                               encoder_attention_bias,
                                                                                               self.key_past,
                                                                                               self.value_past,
                                                                                               batch_valid_length)
            layer_present += cross_layer_present
            if self.post_layernorm_residual:
                x = self.add(middle_output, cross_attn_out)
            else:
                x = self.add(x, cross_attn_out)

        output_x = self.layernorm2(x)
        output_x = F.cast(output_x, self.dtype)
        aux_loss = None
        if self.use_moe:
            mlp_logit, aux_loss = self.output(output_x)
        else:
            mlp_logit = self.output(output_x)

        value_update = None
        key_update = None
        if self.use_past:
            # current key and value
            key_present, value_present = layer_present
            # update key and value calculated this step
            key_update = self.assign(self.key_past, key_present)
            value_update = self.assign(self.value_past, value_present)
            # add dependency for desired execution order
            key_update = F.depend(key_update, key_reset)
            value_update = F.depend(value_update, value_reset)

        # add dependency for desired execution order
        mlp_logit = F.depend(mlp_logit, value_update)
        mlp_logit = F.depend(mlp_logit, key_update)

        # if shape is 3d, we reshape the inputs of the add
        if len(hidden_shape) == 3:
            output_x = P.Reshape()(output_x, hidden_shape)
            mlp_logit = P.Reshape()(mlp_logit, hidden_shape)
            x = P.Reshape()(x, hidden_shape)

            if self.post_layernorm_residual:
                output = self.add_3d(output_x, mlp_logit)
            else:
                output = self.add_3d(x, mlp_logit)
        else:
            if self.post_layernorm_residual:
                output = self.add(output_x, mlp_logit)
            else:
                output = self.add(x, mlp_logit)
            output = F.reshape(output, hidden_shape)

        if self.use_moe:
            return output, layer_present, aux_loss
        return output, layer_present, self_bias, encoder_attention_bias

    def _check_input(self, hidden_states, attention_mask, encoder_output, memory_mask, init_reset, batch_valid_length):
        r"""Check inputs"""
        if not self.use_past or (self.use_past and self.is_first_iteration):
            _check_shape_equal(F.shape(hidden_states), "hidden_states", self.cls_name,
                               [[self.batch_size, self.tgt_seq_length, self.hidden_size],
                                [self.batch_size * self.tgt_seq_length, self.hidden_size]])
            _check_shape_equal(F.shape(attention_mask), "attention_mask", self.cls_name,
                               [self.batch_size, self.tgt_seq_length, self.tgt_seq_length])

        else:
            _check_shape_equal(F.shape(hidden_states), "hidden_states", self.cls_name,
                               [self.batch_size, 1, self.hidden_size])
            _check_shape_equal(F.shape(attention_mask), "attention_mask", self.cls_name,
                               [self.batch_size, 1, self.tgt_seq_length])
        _check_input_dtype(F.dtype(hidden_states), "hidden_states", [mstype.float32, mstype.float16], self.cls_name)
        _check_input_dtype(F.dtype(attention_mask), "attention_mask", [mstype.float32, mstype.float16], self.cls_name)
        if encoder_output is not None:
            _check_shape_equal(F.shape(encoder_output), "encoder_output", self.cls_name,
                               [[self.batch_size, self.src_seq_length, self.hidden_size],
                                [self.batch_size * self.src_seq_length, self.hidden_size]])
            _check_input_dtype(F.dtype(encoder_output), "encoder_output",
                               [mstype.float32, mstype.float16], self.cls_name)
        if memory_mask is not None:
            _check_shape_equal(F.shape(memory_mask), "memory_mask", self.cls_name,
                               [self.batch_size, self.tgt_seq_length, self.src_seq_length])
            _check_input_dtype(F.dtype(memory_mask), "memory_mask",
                               [mstype.float32, mstype.float16], self.cls_name)

        init_reset_is_tensor = isinstance(init_reset, Tensor)
        init_reset_is_default = init_reset is True
        batch_valid_length_is_tensor = isinstance(batch_valid_length, Tensor)
        batch_is_default = batch_valid_length is None
        _check_past_none_input_none(self.use_past, "init_reset", self.cls_name, True, init_reset_is_tensor,
                                    init_reset_is_default)
        _check_past_none_input_none(self.use_past, "batch_valid_length", self.cls_name, None,
                                    batch_valid_length_is_tensor, batch_is_default)

        if self.use_past:
            _check_shape_equal(F.shape(init_reset), "init_reset", self.cls_name, [1])
            _check_input_dtype(F.dtype(init_reset), "init_reset", [mstype.bool_], self.cls_name)
            _check_shape_equal(F.shape(batch_valid_length), "batch_valid_length", self.cls_name, [self.batch_size])
            _check_input_dtype(F.dtype(batch_valid_length), "batch_valid_length", [mstype.int32], self.cls_name)
        return True


def _get_lambda_func(total_layer=None):
    r"""
    A wrapper function of specifying pipeline stage and gradient aggregation fusion. If the total layer
    is not None, for example, set in the transformer model, the pipeline stage setting function will be
    `(layer_id + 0) // (total_layers / parallel_config.pipeline_stage)` for the encoder and,
    `(layer_id + offset) //
    (total_layers / parallel_config.pipeline_stage)` for the decoder, where `offset` is the layers in the encoder.
    """

    def _set_parallel_configure_for_layer(network, layer_id, offset, parallel_config, layers):
        r"""
        Default setting for the pipeline is: `(layer_id + offset) // (layers / pipeline_stage)`.

        Args:
            network(Cell) - Represents the transformer block
            layer_id(int) - Means the layer index for the current module, counts from zero.
            offset(int) - Means the layer_index needs an offset, if there are other modules in the net.
            layers(int) - The total layers used for the model.
        """
        # override the layers
        if total_layer:
            layers = total_layer
        # Used for the pipeline's stages setting
        if layers < parallel_config.pipeline_stage:
            raise ValueError(f"layers {layers} must be larger than pipeline stage {parallel_config.pipeline_stage}")

        pp_dis = max(int(layers / parallel_config.pipeline_stage), 1)
        # the pipeline stage must be in [0, parallel_config.pipeline_stage - 1]
        pp_id = min((layer_id + offset) // pp_dis, parallel_config.pipeline_stage - 1)
        network.pipeline_stage = pp_id

        # Used for optimizer's fusion tag
        dis = max(int(layers / parallel_config.gradient_aggregation_group), 1)
        network.set_comm_fusion(int((layer_id + offset) / dis) + 1)
        # Used for enabling recomputation of the block
        if isinstance(parallel_config.recompute, bool):
            if parallel_config.recompute:
                network.recompute()
        else:
            if parallel_config.recompute.recompute:
                paralel_op_comm_compute = parallel_config.recompute.parallel_optimizer_comm_recompute
                network.recompute(parallel_optimizer_comm_recompute=paralel_op_comm_compute,
                                  mp_comm_recompute=parallel_config.recompute.mp_comm_recompute,
                                  recompute_slice_activation=parallel_config.recompute.recompute_slice_activation)

    return _set_parallel_configure_for_layer


class TransformerEncoder(Cell):
    """The TransformerEncoder Cell"""
    def __init__(self,
                 batch_size,
                 num_layers,
                 hidden_size,
                 ffn_hidden_size,
                 seq_length,
                 num_heads,
                 attention_dropout_rate=0.1,
                 hidden_dropout_rate=0.1,
                 hidden_act='gelu',
                 post_layernorm_residual=False,
                 layernorm_compute_type=mstype.float32,
                 softmax_compute_type=mstype.float32,
                 param_init_type=mstype.float32,
                 lambda_func=None,
                 offset=0,
                 use_past=False,
                 moe_config=default_moe_config,
                 parallel_config=default_transformer_config):
        super(TransformerEncoder, self).__init__()
        _check_moe_config(moe_config, parallel_config)
        self.use_moe = (moe_config.expert_num > 1)
        self.add = P.Add()
        self.aux_loss = Tensor(0.0, mstype.float32)
        self.num_layers = num_layers
        self.blocks = nn.CellList()
        for i in range(num_layers):
            block = TransformerEncoderLayer(hidden_size=hidden_size,
                                            batch_size=batch_size,
                                            ffn_hidden_size=ffn_hidden_size,
                                            seq_length=seq_length,
                                            attention_dropout_rate=attention_dropout_rate,
                                            hidden_dropout_rate=hidden_dropout_rate,
                                            layernorm_compute_type=layernorm_compute_type,
                                            softmax_compute_type=softmax_compute_type,
                                            num_heads=num_heads,
                                            hidden_act=hidden_act,
                                            has_bias=(i == 0),
                                            post_layernorm_residual=post_layernorm_residual,
                                            param_init_type=param_init_type,
                                            use_past=use_past,
                                            moe_config=moe_config,
                                            parallel_config=parallel_config.moe_parallel_config if self.use_moe
                                            else parallel_config.dp_mp_config)
            # If the user doesn't pass the fusion function, use the default one
            if not lambda_func:
                lambda_func = _get_lambda_func()

            lambda_func(block, layer_id=i, layers=num_layers,
                        offset=offset, parallel_config=parallel_config)
            self.blocks.append(block)

        if _get_parallel_mode() in (ParallelMode.AUTO_PARALLEL,) and _is_sharding_propagation():
            pass
        elif _get_parallel_mode() not in (ParallelMode.AUTO_PARALLEL,):
            logger.warning("For parallel mode, sharding propagation is recommended, you can use it by setting "
                           "'set_auto_parallel_context(parallel_mode=ParallelMode.AUTO_PARALLEL, "
                           "search_mode=\"sharding_propagation\")' and "
                           "'set_algo_parameters(elementwise_op_strategy_follow=False, fully_use_devices=False)'")
        else:
            raise RuntimeError(f"The {self.cls_name} only support sharding propagation or "
                               f"semi-auto parallel mode now.")

    def construct(self, hidden_states, attention_mask, init_reset=True, batch_valid_length=None):
        """The forward process of the encoder"""
        present_layer = ()
        attention_bias = None
        if self.use_moe:
            accum_loss = self.aux_loss
            for i in range(self.num_layers):
                hidden_states, present, aux_loss = self.blocks[i](hidden_states,
                                                                  attention_mask,
                                                                  init_reset,
                                                                  batch_valid_length)
                present_layer = present_layer + (present,)
                accum_loss = self.add(accum_loss, aux_loss)
            return hidden_states, present_layer, accum_loss

        for i in range(self.num_layers):
            hidden_states, present, attention_bias = self.blocks[i](hidden_states,
                                                                    attention_mask,
                                                                    attention_bias,
                                                                    init_reset,
                                                                    batch_valid_length)
            present_layer = present_layer + (present,)

        return hidden_states, present_layer


class TransformerDecoder(Cell):
    """The TransformerDecoder cell"""
    def __init__(self,
                 num_layers,
                 batch_size,
                 hidden_size,
                 ffn_hidden_size,
                 src_seq_length,
                 tgt_seq_length,
                 num_heads,
                 attention_dropout_rate=0.1,
                 hidden_dropout_rate=0.1,
                 post_layernorm_residual=False,
                 layernorm_compute_type=mstype.float32,
                 softmax_compute_type=mstype.float32,
                 param_init_type=mstype.float32,
                 hidden_act='gelu',
                 lambda_func=None,
                 use_past=False,
                 offset=0,
                 moe_config=default_moe_config,
                 parallel_config=default_transformer_config):
        super(TransformerDecoder, self).__init__()
        self.add = P.Add()
        self.aux_loss = Tensor(0.0, mstype.float32)
        self.num_layers = num_layers
        self.blocks = nn.CellList()
        _check_moe_config(moe_config, parallel_config)
        self.use_moe = (moe_config.expert_num > 1)
        for i in range(num_layers):
            block = TransformerDecoderLayer(hidden_size=hidden_size,
                                            batch_size=batch_size,
                                            ffn_hidden_size=ffn_hidden_size,
                                            src_seq_length=src_seq_length,
                                            tgt_seq_length=tgt_seq_length,
                                            attention_dropout_rate=attention_dropout_rate,
                                            hidden_dropout_rate=hidden_dropout_rate,
                                            num_heads=num_heads,
                                            layernorm_compute_type=layernorm_compute_type,
                                            softmax_compute_type=softmax_compute_type,
                                            hidden_act=hidden_act,
                                            use_past=use_past,
                                            has_bias=(i == 0),
                                            param_init_type=param_init_type,
                                            post_layernorm_residual=post_layernorm_residual,
                                            moe_config=moe_config,
                                            parallel_config=parallel_config.moe_parallel_config if self.use_moe
                                            else parallel_config.dp_mp_config)
            # If the user doesn't pass the fusion function, use the default one
            if not lambda_func:
                lambda_func = _get_lambda_func()

            lambda_func(block, layer_id=i, layers=num_layers,
                        offset=offset, parallel_config=parallel_config)

            self.blocks.append(block)

        if _get_parallel_mode() in (ParallelMode.AUTO_PARALLEL,) and _is_sharding_propagation():
            pass
        elif _get_parallel_mode() not in (ParallelMode.AUTO_PARALLEL,):
            logger.warning("For parallel mode, sharding propagation is recommended, you can use it by setting "
                           "'set_auto_parallel_context(parallel_mode=ParallelMode.AUTO_PARALLEL, "
                           "search_mode=\"sharding_propagation\")' and "
                           "'set_algo_parameters(elementwise_op_strategy_follow=False, fully_use_devices=False)'")
        else:
            raise RuntimeError(f"The {self.cls_name} only support sharding propagation or "
                               f"semi-auto parallel mode now.")

    def construct(self, hidden_states, attention_mask, encoder_output=None, memory_mask=None,
                  init_reset=True, batch_valid_length=None):
        """For forward process of the decoder"""
        present_layer = ()
        self_bias = None
        encoder_decoder_bias = None
        if self.use_moe:
            accum_loss = self.aux_loss
            for i in range(self.num_layers):
                hidden_states, present, aux_loss = self.blocks[i](hidden_states,
                                                                  attention_mask,
                                                                  encoder_output,
                                                                  memory_mask,
                                                                  init_reset,
                                                                  batch_valid_length)
                present_layer = present_layer + (present,)
                accum_loss = self.add(accum_loss, aux_loss)
            return hidden_states, present_layer, accum_loss

        # Loop through each self-attention layer
        for i in range(self.num_layers):
            hidden_states, present, self_bias, encoder_decoder_bias = self.blocks[i](hidden_states,
                                                                                     attention_mask,
                                                                                     encoder_output,
                                                                                     memory_mask,
                                                                                     self_bias,
                                                                                     encoder_decoder_bias,
                                                                                     init_reset,
                                                                                     batch_valid_length)
            present_layer = present_layer + (present,)

        return hidden_states, present_layer
