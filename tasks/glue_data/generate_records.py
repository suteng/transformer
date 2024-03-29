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
"""Utility functions for GLUE classification tasks."""

import csv
import os
import io
import argparse
from collections import namedtuple
import numpy as np

from mindspore.mindrecord import FileWriter
from mindspore.log import logging
import transformer.tokenization.bert_tokenization as tokenization


class InputExample:
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
          guid: Unique id for the example.
          text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
          text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
          label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class PaddingInputExample:
    """Fake example so the num input examples is a multiple of the batch size.

    When running eval/predict on the TPU, we need to pad the number of examples
    to be a multiple of the batch size, because the TPU requires a fixed batch
    size. The alternative is to drop the last batch, which is bad because it means
    the entire output data won't be generated.

    We use this class instead of `None` because treating `None` as padding
    battches could cause silent errors.
    """


class InputFeatures:
    """A single set of features of data."""

    def __init__(self,
                 input_ids,
                 input_mask,
                 segment_ids,
                 label_id,
                 guid=None,
                 example_id=None,
                 is_real_example=True):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.example_id = example_id
        self.guid = guid
        self.is_real_example = is_real_example


class DataProcessor:
    """Base class for data converters for sequence classification data sets."""

    def __init__(self, use_spm, do_lower_case):
        super(DataProcessor, self).__init__()
        self.use_spm = use_spm
        self.do_lower_case = do_lower_case

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_test_examples(self, data_dir):
        """Gets a collection of `InputExample`s for prediction."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with io.open(input_file, "r", encoding="utf8") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                lines.append(line)
            return lines

    def process_text(self, text):
        if self.use_spm:
            token_text = tokenization.preprocess_text(text, do_lower_case=self.do_lower_case)
        else:
            token_text = tokenization.convert_to_unicode(text)
        return token_text


class MnliProcessor(DataProcessor):
    """Processor for the MultiNLI data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MNLI", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MNLI", "dev_matched.tsv")),
            "dev_matched")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MNLI", "test_matched.tsv")),
            "test")

    def get_labels(self):
        """See base class."""
        return ["contradiction", "entailment", "neutral"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            # Note(mingdachen): We will rely on this guid for GLUE submission.
            guid = self.process_text(line[0])
            text_a = self.process_text(line[8])
            text_b = self.process_text(line[9])
            if set_type == "test":
                label = "contradiction"
            else:
                label = self.process_text(line[-1])
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class MisMnliProcessor(MnliProcessor):
    """Processor for the Mismatched MultiNLI data set (GLUE version)."""

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MNLI", "dev_mismatched.tsv")),
            "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MNLI", "test_mismatched.tsv")),
            "test")


class MrpcProcessor(DataProcessor):
    """Processor for the MRPC data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MRPC", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MRPC", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "MRPC", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, i)
            text_a = self.process_text(line[3])
            text_b = self.process_text(line[4])
            if set_type == "test":
                guid = line[0]
                label = "0"
            else:
                label = self.process_text(line[0])
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class ColaProcessor(DataProcessor):
    """Processor for the CoLA data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "CoLA", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "CoLA", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "CoLA", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            # Only the test set has a header
            if set_type == "test" and i == 0:
                continue
            guid = "%s-%s" % (set_type, i)
            if set_type == "test":
                guid = line[0]
                text_a = self.process_text(line[1])
                label = "0"
            else:
                text_a = self.process_text(line[3])
                label = self.process_text(line[1])
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
        return examples


class Sst2Processor(DataProcessor):
    """Processor for the SST-2 data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "SST-2", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "SST-2", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "SST-2", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            if set_type != "test":
                guid = "%s-%s" % (set_type, i)
                text_a = self.process_text(line[0])
                label = self.process_text(line[1])
            else:
                guid = self.process_text(line[0])
                # guid = "%s-%s" % (set_type, line[0])
                text_a = self.process_text(line[1])
                label = "0"
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
        return examples


class StsbProcessor(DataProcessor):
    """Processor for the STS-B data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "STS-B", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "STS-B", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "STS-B", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return [None]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = self.process_text(line[0])
            # guid = "%s-%s" % (set_type, line[0])
            text_a = self.process_text(line[7])
            text_b = self.process_text(line[8])
            if set_type != "test":
                label = float(line[-1])
            else:
                label = 0
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class QqpProcessor(DataProcessor):
    """Processor for the QQP data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "QQP", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "QQP", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "QQP", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = line[0]
            # guid = "%s-%s" % (set_type, line[0])
            if set_type != "test":
                try:
                    text_a = self.process_text(line[3])
                    text_b = self.process_text(line[4])
                    label = self.process_text(line[5])
                except IndexError:
                    continue
            else:
                text_a = self.process_text(line[1])
                text_b = self.process_text(line[2])
                label = "0"
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class QnliProcessor(DataProcessor):
    """Processor for the QNLI data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "QNLI", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "QNLI", "dev.tsv")),
            "dev_matched")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "QNLI", "test.tsv")),
            "test_matched")

    def get_labels(self):
        """See base class."""
        return ["entailment", "not_entailment"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = self.process_text(line[0])
            # guid = "%s-%s" % (set_type, line[0])
            text_a = self.process_text(line[1])
            text_b = self.process_text(line[2])
            if set_type == "test_matched":
                label = "entailment"
            else:
                label = self.process_text(line[-1])
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class RteProcessor(DataProcessor):
    """Processor for the RTE data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "RTE", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "RTE", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "RTE", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return ["entailment", "not_entailment"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = self.process_text(line[0])
            # guid = "%s-%s" % (set_type, line[0])
            text_a = self.process_text(line[1])
            text_b = self.process_text(line[2])
            if set_type == "test":
                label = "entailment"
            else:
                label = self.process_text(line[-1])
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class WnliProcessor(DataProcessor):
    """Processor for the WNLI data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "WNLI", "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "WNLI", "dev.tsv")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "WNLI", "test.tsv")), "test")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = self.process_text(line[0])
            # guid = "%s-%s" % (set_type, line[0])
            text_a = self.process_text(line[1])
            text_b = self.process_text(line[2])
            if set_type != "test":
                label = self.process_text(line[-1])
            else:
                label = "0"
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class AXProcessor(DataProcessor):
    """Processor for the AX data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "diagnostic", "diagnostic.tsv")),
            "test")

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """See base class."""
        return ["contradiction", "entailment", "neutral"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            # Note(mingdachen): We will rely on this guid for GLUE submission.
            guid = self.process_text(line[0])
            text_a = self.process_text(line[1])
            text_b = self.process_text(line[2])
            if set_type == "test":
                label = "contradiction"
            else:
                label = self.process_text(line[-1])
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class ClassificationConverter:
    """Convert the examples to classification format"""
    def __init__(self, label_list, max_seq_length):
        nlp_schema = {
            "input_ids": {"type": "int64", "shape": [-1]},
            "input_mask": {"type": "int64", "shape": [-1]},
            "segment_ids": {"type": "int64", "shape": [-1]},
            "label_ids": {"type": "int64", "shape": [-1]},
        }
        self.schema = nlp_schema
        self.label_list = label_list
        self.max_seq_length = max_seq_length

    def convert_single_example(self, example, tokenizer, task_name):
        """Converts a single `InputExample` into a single `InputFeatures`."""
        max_seq_length = self.max_seq_length
        label_list = self.label_list

        if isinstance(example, PaddingInputExample):
            return {
                "input_ids": np.zeros(max_seq_length, np.int64),
                "input_mask": np.zeros(max_seq_length, np.int64),
                "segment_ids": np.zeros(max_seq_length, np.int64),
                "label_ids": np.array([0], np.int64),
                "is_real_example": np.array([False], dtype=np.int64),
            }

        if task_name != "sts-b":
            label_map = {}
            for (i, label) in enumerate(label_list):
                label_map[label] = i

        tokens_a = tokenizer.tokenize(example.text_a)
        tokens_b = None
        if example.text_b:
            tokens_b = tokenizer.tokenize(example.text_b)

        if tokens_b:
            # Modifies `tokens_a` and `tokens_b` in place so that the total
            # length is less than the specified length.
            # Account for [CLS], [SEP], [SEP] with "- 3"
            _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
        else:
            # Account for [CLS] and [SEP] with "- 2"
            if len(tokens_a) > max_seq_length - 2:
                tokens_a = tokens_a[0:(max_seq_length - 2)]

        # The convention in ALBERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids: 0     0  0    0    0     0       0 0     1  1  1  1   1 1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids: 0     0   0   0  0     0 0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambiguously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.
        tokens = []
        segment_ids = []
        tokens.append("[CLS]")
        segment_ids.append(0)
        for token in tokens_a:
            tokens.append(token)
            segment_ids.append(0)
        tokens.append("[SEP]")
        segment_ids.append(0)

        if tokens_b:
            for token in tokens_b:
                tokens.append(token)
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        if task_name != "sts-b":
            label_id = label_map[example.label]
        else:
            label_id = example.label

        Record = namedtuple(
            'Record',
            ['input_ids', 'input_mask', 'segment_ids', 'label_id', 'is_real_example'])

        record = Record(
            input_ids=input_ids,
            input_mask=input_mask,
            segment_ids=segment_ids,
            label_id=label_id,
            is_real_example=True)

        sample = {
            "input_ids": np.array(record.input_ids, dtype=np.int64),
            "input_mask": np.array(record.input_mask, dtype=np.int64),
            "segment_ids": np.array(record.segment_ids, dtype=np.int64),
            "label_ids": np.array([record.label_id], dtype=np.int64),
            "is_real_example": np.array([record.is_real_example], dtype=np.int64),
        }
        return sample


class TranslationConverter:
    """Convert the examples to text to text format"""
    def __init__(self, **kwargs):
        self.src_seq_length = kwargs.get('src_seq_length')
        self.tgt_seq_length = kwargs.get('tgt_seq_length')
        nlp_schema = {
            "source_eos_ids": {"type": "int32", "shape": [-1]},
            "source_eos_mask": {"type": "int32", "shape": [self.src_seq_length, self.src_seq_length]},
            "target_sos_ids": {"type": "int32", "shape": [-1]},
            "target_sos_mask": {"type": "int32", "shape": [self.tgt_seq_length, self.tgt_seq_length]},
            "target_eos_ids": {"type": "int32", "shape": [-1]},
            "target_eos_mask": {"type": "int32", "shape": [-1]},
        }
        self.schema = nlp_schema

    def _prepand_prefix(self, texta):
        text = f'cola sentence: {texta}'
        return text

    def _generate_zeros_feature(self):
        sample = {
            "source_eos_ids": np.zeros(self.src_seq_length, dtype=np.int32),
            "source_eos_mask": np.zeros((self.src_seq_length, self.src_seq_length), dtype=np.int32),
            "target_sos_ids": np.zeros(self.tgt_seq_length, dtype=np.int32),
            "target_sos_mask": np.zeros((self.tgt_seq_length, self.tgt_seq_length), dtype=np.int32),
            "target_eos_ids": np.zeros(self.tgt_seq_length, dtype=np.int32),
            "target_eos_mask": np.zeros(self.tgt_seq_length, dtype=np.int32),
        }
        return sample

    def create_label_mask(self, labels):
        """
        Create the label masks
        """
        shape = len(labels)
        return np.ones(shape).astype(np.int32)

    def create_attention_mask_bidirection(self, input_ids):
        """
        According to the inputs, generates the output
        returns [[1, 1, 0]]
                 [1, 1, 0]
                 [0, 0, 0]]
        """
        if len(np.array(input_ids).shape) != 1:
            raise ValueError("Expect the input_ids to be 1d array")
        mask_a = np.expand_dims(np.not_equal(input_ids, 0).astype(np.int32), 0)
        mask_b = mask_a.T
        mask = mask_b * mask_a
        return mask

    def create_attention_single_direction(self, input_ids):
        """
        According to the inputs, generates the output
        returns [[1, 0, 0]]
                 [1, 1, 0]
                 [0, 0, 0]]
        """
        if len(np.array(input_ids).shape) != 1:
            raise ValueError("Expect the input_ids to be 1d array")
        seq_length = len(input_ids)
        mask_a = np.expand_dims(np.not_equal(input_ids, 0).astype(np.int32), 0)
        down_matrix = np.tril(np.ones(shape=(seq_length, seq_length)))
        mask_b = mask_a.T
        mask = mask_b * mask_a
        mask = mask * down_matrix
        return mask

    def convert_single_example(self, example, tokenizer, task_name):
        """
        Converts a single `InputExample` into a single `InputFeatures`.
        For example, the convert CoLA Task to the format like
        Input text: John made Bill master of himself
        Processed input: cola sentence: John made Bill Master of himself
        Original target: 1
        Processed target: acceptable
        """

        if isinstance(example, PaddingInputExample):
            return self._generate_zeros_feature()

        example.text_a = self._prepand_prefix(example.text_a)
        tokens_a = tokenizer.tokenize(example.text_a)

        if len(tokens_a) > self.src_seq_length:
            tokens_a = tokens_a[:self.src_seq_length]

        if task_name != 'cola':
            label = example.label
        else:
            label_mapper = {"0": 'negative', "1": 'positive'}
            label = label_mapper[example.label]
        label = tokenizer.convert_tokens_to_ids(tokenizer.tokenize('[START] ' + label + ' [EOD]'))
        if len(label) > self.tgt_seq_length + 1:
            label = label[:self.tgt_seq_length + 1]
        else:
            label += [0] * (self.tgt_seq_length + 1 - len(label))

        source_eos_ids = tokenizer.convert_tokens_to_ids(tokens_a)
        if len(source_eos_ids) > self.src_seq_length:
            source_eos_ids = source_eos_ids[:self.src_seq_length]
        else:
            source_eos_ids += [0] * (self.src_seq_length - len(source_eos_ids))
        source_eos_masks = self.create_attention_mask_bidirection(source_eos_ids)

        target_sos_ids = label[:-1]
        tgt_sos_masks = self.create_attention_single_direction(target_sos_ids)
        target_eos_ids = label[1:]
        target_eos_masks = self.create_label_mask(target_eos_ids)

        sample = {
            "source_eos_ids": np.array(source_eos_ids, dtype=np.int32),
            "source_eos_mask": np.array(source_eos_masks, dtype=np.int32),
            "target_sos_ids": np.array(target_sos_ids, dtype=np.int32),
            "target_sos_mask": np.array(tgt_sos_masks, dtype=np.int32),
            "target_eos_ids": np.array(target_eos_ids, dtype=np.int32),
            "target_eos_mask": np.array(target_eos_masks, dtype=np.int32),
        }
        return sample


class GeneratorConverter:
    """Convert the examples to text to text format"""
    def __init__(self, **kwargs):
        self.max_seq_length = kwargs.get('max_seq_length')
        self.task_name = kwargs.get('task_name')
        nlp_schema = {
            "input_ids": {"type": "int32", "shape": [-1]},
        }
        self.schema = nlp_schema

    def _prepand_prefix(self, texta):
        text = f'cola sentence: {texta}'
        return text

    def _generate_zeros_feature(self):
        sample = {
            "source_eos_ids": np.zeros(self.max_seq_length, dtype=np.int32),
        }
        return sample

    def convert_single_example(self, example, tokenizer, task_name):
        """
        Converts a single `InputExample` into a single `InputFeatures`.
        For example, the convert CoLA Task to the format like
        Input text: John made Bill master of himself
        Processed input: cola sentence: John made Bill Master of himself
        Original target: 1
        Processed target: acceptable
        """

        if isinstance(example, PaddingInputExample):
            return self._generate_zeros_feature()

        example.text_a = self._prepand_prefix(example.text_a)
        tokens_a = tokenizer.tokenize(example.text_a)

        if len(tokens_a) > self.max_seq_length:
            tokens_a = tokens_a[:self.max_seq_length]

        if task_name != 'cola':
            label = example.label
        else:
            label_mapper = {"0": 'negative', "1": 'positive'}
            label = label_mapper[example.label]
        label = tokenizer.tokenize(label + ' [EOD]')
        text = tokenizer.convert_tokens_to_ids(tokens_a + label)
        if len(text) > self.max_seq_length:
            text = text[:self.max_seq_length]
        else:
            text += [0] * (self.max_seq_length - len(text))
        sample = {
            "input_ids": np.array(text, dtype=np.int32),
        }
        return sample


def file_based_convert_examples_to_features(
        examples, converter, tokenizer, output_file, task_name, shard_num):
    """Convert a set of `InputExample`s to a MindRecord file."""
    writer = FileWriter(file_name=output_file, shard_num=shard_num)

    writer.add_schema(converter.schema, "Preprocessed dataset")
    data = []

    for (ex_index, example) in enumerate(examples):
        if ex_index % 10000 == 0:
            logging.info("Writing example %d of %d" % (ex_index, len(examples)))
        record = converter.convert_single_example(example, tokenizer, task_name)
        data.append(record)
    print(f"Processed total {len(data)} examples.")
    writer.write_raw_data(data)
    writer.commit()


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def get_argument():
    """Get argument"""
    parser = argparse.ArgumentParser(description="read dataset and save it to minddata")
    parser.add_argument("--task_name", type=str, default="", help="task type to preprocess")
    parser.add_argument("--vocab_path", type=str, default="", help="vocab file")
    parser.add_argument("--spm_model_file", type=str, default=None, help="spm file")
    parser.add_argument("--max_seq_length", type=int, default=128,
                        help="The maximum total input sequence length after WordPiece tokenization. "
                             "Sequences longer than this will be truncated, and sequences shorter "
                             "than this will be padded.")
    parser.add_argument("--do_lower_case", type=str, default="true",
                        help="Whether to lower case the input text. "
                             "Should be True for uncased models and False for cased models.")
    parser.add_argument("--input_dir", type=str, default="", help="raw data file")
    parser.add_argument("--output_dir", type=str, default="", help="minddata file")
    parser.add_argument("--shard_num", type=int, default=0, help="output file shard number")
    parser.add_argument("--do_train", type=str, default="false",
                        help="Whether the processing dataset is training dataset.")
    parser.add_argument("--do_eval", type=str, default="false",
                        help="Whether the processing dataset is dev dataset.")
    parser.add_argument("--do_pred", type=str, default="false",
                        help="Whether the processing dataset is test dataset.")
    parser.add_argument("--format", default='bert', choices=['bert', 'gpt', 't5'],
                        help="Process the dataset with t5 format.")
    parser.add_argument("--src_seq_length", type=int, default=128,
                        help="The maximum total input sequence length for t5 format.")
    parser.add_argument("--tgt_seq_length", type=int, default=128,
                        help="The maximum total input sequence length for t5 format.")
    args_opt = parser.parse_args()

    return args_opt


def main():
    processors = {
        "cola": ColaProcessor,
        "mnli": MnliProcessor,
        "mismnli": MisMnliProcessor,
        "mrpc": MrpcProcessor,
        "rte": RteProcessor,
        "sst-2": Sst2Processor,
        "sts-b": StsbProcessor,
        "qqp": QqpProcessor,
        "qnli": QnliProcessor,
        "wnli": WnliProcessor,
    }
    convert = {'bert': ClassificationConverter, 'gpt': GeneratorConverter, 't5': TranslationConverter}
    args_opt = get_argument()
    task_name = args_opt.task_name.lower()
    processor = processors[task_name](
        use_spm=bool(args_opt.spm_model_file),
        do_lower_case=args_opt.do_lower_case)

    label_list = processor.get_labels()
    print("label_list: ", label_list)
    output_dir = args_opt.output_dir

    if not os.path.exists(output_dir):
        print(f"mkdir -p {output_dir}")
        os.makedirs(output_dir, exist_ok=True)

    tokenizer = tokenization.FullTokenizer(
        vocab_file=args_opt.vocab_path, do_lower_case=args_opt.do_lower_case, spm_model_file=args_opt.spm_model_file)

    convert_class = convert[args_opt.format]
    if args_opt.format == 't5':
        print("As the format is t5 format, the args_opt.max_seq_length will be invalid, src_seq_length "
              "and tgt_seq_length will task effective.")
        convert_args = dict(src_seq_length=args_opt.src_seq_length,
                            tgt_seq_length=args_opt.tgt_seq_length,
                            task_name=task_name)
    else:
        convert_args = dict(label_list=label_list, max_seq_length=args_opt.max_seq_length, task_name=task_name)
    converter = convert_class(**convert_args)
    if args_opt.do_train:
        print("input_dir: ", args_opt.input_dir)
        train_examples = processor.get_train_examples(args_opt.input_dir)
        train_file = os.path.join(output_dir, task_name + "_train.mindrecord")
        print("train_file: ", train_file)
        if not os.path.exists(train_file):
            file_based_convert_examples_to_features(
                train_examples, converter, tokenizer,
                train_file, task_name, args_opt.shard_num)

    if args_opt.do_eval:
        print("input_dir: ", args_opt.input_dir)
        dev_examples = processor.get_dev_examples(args_opt.input_dir)
        eval_file = os.path.join(output_dir, task_name + "_dev.mindrecord")
        print("eval_file: ", eval_file)
        if not os.path.exists(eval_file):
            file_based_convert_examples_to_features(
                dev_examples, converter, tokenizer,
                eval_file, task_name, args_opt.shard_num)

    if args_opt.do_pred:
        test_examples = processor.get_test_examples(args_opt.input_dir)
        test_file = os.path.join(output_dir, task_name + "_test.mindrecord")
        print("test_file: ", test_file)
        if not os.path.exists(test_file):
            file_based_convert_examples_to_features(
                test_examples, converter, tokenizer,
                test_file, task_name, args_opt.shard_num)

    print("Preprocessed done!")

if __name__ == "__main__":
    main()
