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
TopK for text generation
"""
import copy

import numpy as np
import mindspore.common.dtype as mstype
from mindspore.common.tensor import Tensor
from mindspore.ops import operations as P


def topk_fun(logits, topk=5):
    """Get topk"""
    target_column = logits[0].tolist()
    sorted_array = [(k, v) for k, v in enumerate(target_column)]
    sorted_array.sort(key=lambda x: x[1], reverse=True)
    topk_array = sorted_array[:topk]
    index, value = zip(*topk_array)
    index = np.array([index])
    value = np.array([value])
    return value, index


def sampler(log_probs_revised, top_p, top_k_num, use_pynative=False):
    """Convert the log_probs to probability"""
    if use_pynative:
        logits = P.Pow()(np.e, Tensor(log_probs_revised, mstype.float32))
    else:
        logits = np.power(np.e, np.array(log_probs_revised, np.float32))

    # If top_p is less than 1.0, use top_p sampling
    if top_p < 1.0:
        # Only consider the 5000 largest logits to reduce computation
        if use_pynative:
            sorted_logits, index = P.TopK(sorted=True)(logits, 5000)
            cumsum_logits = P.CumSum()(sorted_logits, 1)
            cumsum_logits = cumsum_logits.asnumpy()
            index = index.asnumpy()
            sorted_logits = sorted_logits.asnumpy()
        else:
            sorted_logits, index = topk_fun(logits, 5000)
            cumsum_logits = np.cumsum(sorted_logits, 1)
        cumsum_logits = cumsum_logits[0]
        index = index[0]
        sorted_logits = sorted_logits[0]
        top_p_num = sum(cumsum_logits < top_p) + 1
        # In case the probability is smooth, the sum of 5000 largest probabilities are not large enough
        if top_p_num == 0:
            top_p_num = 5000
        # Get the corresponding probs and indices
        probs = sorted_logits[:top_p_num]
        p_args = index[:top_p_num]
        p = probs / sum(probs)
        # if top_p is set to 1.0, use top_k sampling
    else:
        # Get the corresponding probs and indices
        if use_pynative:
            probs, p_args = P.TopK(sorted=True)(logits, top_k_num)
            probs = probs.asnumpy()
            p_args = p_args.asnumpy()
        else:
            probs, p_args = topk_fun(logits, top_k_num)
        probs = probs[0]
        p_args = p_args[0]
        # Avoid rounding error
        if sum(probs) == 0:
            probs = np.array([1 / top_k_num for _ in range(top_k_num)])
        p = probs / sum(probs)
    return p, p_args


def generate(model,
             end_token,
             origin_inputs,
             model_origin_max_length,
             max_generate_length,
             vocab_size,
             cache_encoder,
             config):
    """
    Text generation given the model and origin inputs

    Inputs:
        model: The model to run the prediction
        end_token(int): The model will stop generating the words when it reaches the end_token.
        origin_inputs(list): The prompt for generation, should be a list of ids.
        model_origin_max_length(int): The sequence length of the model trained.
        max_generate_length(int):  The maximum of generated length.
        vocab_size(int): The vocabulary length of the model.
        config: Inference configurations.

    Returns:
        outputs: the ids for the generated text
    """
    # Get configurations for inference
    frequency_penalty = config.frequency_penalty
    presence_penalty = config.presence_penalty
    top_p = config.top_p
    top_k_num = config.top_k_num
    use_pynative = False

    _, valid_length = origin_inputs.shape
    # If target length exceeds model_origin_max_length, use model_origin_max_length instead
    target_length = valid_length + max_generate_length
    target_length = model_origin_max_length if target_length > model_origin_max_length else target_length

    # A list of the frequency of each token
    frequency_list = np.array([[0 for _ in range(vocab_size)]])
    pad_length = model_origin_max_length - origin_inputs.shape[-1]
    # Pad original inputs to model_origin_max_length
    input_ids = np.pad(origin_inputs, ((0, 0), (0, pad_length)), 'constant', constant_values=(0, 0))
    input_mask = np.zeros_like(input_ids)
    input_mask[0][:valid_length] = 1
    config.logger.info(f"input_ids is {input_ids}")

    encoder_output = None
    encoder_mask = None
    encoder_mask = None
    if cache_encoder:
        # When do encoder and decoder prediction, the encoder can be cached to speed up the inference
        inputs = Tensor(input_ids, mstype.int32)
        encoder_mask = copy.deepcopy(input_mask)
        encoder_output = model.predict(inputs, Tensor(encoder_mask, mstype.float32))
        input_ids = [[0]]
        input_ids = np.pad(input_ids, ((0, 0), (0, config.model['max_decode_length'] - 1)),
                           'constant', constant_values=(0, 0))
        target_mask = np.zeros_like(input_ids)
        target_mask[0, 0] = 1
        # As the decoder is generating from [START] token
        valid_length = 1
    # A single loop generates one token, loop until reaching target model_origin_max_length or generating eod token
    while valid_length < target_length:
        inputs = Tensor(input_ids, mstype.int32)
        # Indicate the exact token position
        current_index = valid_length - 1 if valid_length - 1 > 0 else 0
        current_index = Tensor([current_index], mstype.int32)
        # Call a single inference
        if cache_encoder:
            # view inputs as target_ids
            log_probs = model.predict(None, Tensor(encoder_mask, mstype.float32), current_index, encoder_output, inputs,
                                      Tensor(target_mask, mstype.float32))
        else:
            log_probs = model.predict(inputs, Tensor(input_mask, mstype.float32))
        # Get the revised log_probs considering frequency and presence penalty to eliminate duplicate
        # in generated results
        log_probs = log_probs.asnumpy().reshape(1, vocab_size)
        log_probs_revised = log_probs - frequency_list * frequency_penalty - (frequency_list > 0) * presence_penalty

        p, p_args = sampler(log_probs_revised, top_p, top_k_num, use_pynative)
        # Random select a token as final output for this round
        target_index = np.random.choice(len(p), p=p)
        # Stop judgment
        if p_args[target_index] == end_token or valid_length == target_length - 1:
            outputs = input_ids
            break

        # update frequency list
        target = p_args[target_index]
        frequency_list[0][target] = frequency_list[0][target] + 1
        # Modify input_ids with newly generated token
        input_ids[0][valid_length] = p_args[target_index]
        if cache_encoder:
            target_mask[0][valid_length] = 1
        valid_length += 1
        input_mask[0][valid_length-1] = 1
    # Return valid outputs out of padded outputs
    length = np.sum(outputs != 0)
    outputs = outputs[0][:length]
    return outputs
