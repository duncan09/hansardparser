"""

Todos:

    TODO: implement class labels for targets, rather than using source vocab.
s
"""
import os
import re
import json
import tensorflow as tf
import pandas as pd

from tensor2tensor.data_generators import problem
from tensor2tensor.data_generators import text_problems, text_encoder, generator_utils
from tensor2tensor.layers import modalities
from tensor2tensor.utils import registry

from . import config
from .utils import get_line_context, normalize_text, split_lines, span2bio, bio2span

CLASS_LABELS = ['B', 'I', 'O']


@registry.register_problem
class HansardLineSpeakerSpan(text_problems.Text2TextProblem):
    """Predict the tokens in a Hansard transcript line that are part of a speaker
    name."""

    CONTEXT_SEPARATOR = "<EOC>"
    CONTEXT_SEPARATOR_ID = 2

    @property
    def additional_reserved_tokens(self):
        return [self.CONTEXT_SEPARATOR]

    @property
    def vocab_type(self):
        return text_problems.VocabType.SUBWORD

    @property
    def approx_vocab_size(self):
        """Approximate vocab size to generate. Only for VocabType.SUBWORD."""
        return 2**16  # ~64k

    @property
    def is_generate_per_split(self):
        return True

    @property
    def dataset_splits(self):
        """Splits of data to produce and number of output shards for each."""
        # 10% evaluation data
        return [{
            "split": problem.DatasetSplit.TRAIN,
            "shards": 9,
        }, {
            "split": problem.DatasetSplit.EVAL,
            "shards": 1,
        }]

    @property
    def targets_encoder(self):
        return text_encoder.TokenTextEncoder(vocab_filename=None, vocab_list=CLASS_LABELS)
        # return text_encoder.ByteTextEncoder()


    def feature_encoders(self, data_dir):
        encoder = self.get_or_create_vocab(data_dir, None, force_get=True)
        encoders = {'inputs': encoder, 'context': encoder, 'targets': self.targets_encoder}
        return encoders


    def generate_text_for_vocab(self, data_dir, tmp_dir):
        for i, sample in enumerate(self.generate_samples(data_dir, tmp_dir, problem.DatasetSplit.TRAIN)):
            yield sample["inputs"]
            yield sample["context"]
            # yield sample["targets"]
            if self.max_samples_for_vocab and (i + 1) >= self.max_samples_for_vocab:
                break


    def generate_encoded_samples(self, data_dir, tmp_dir, dataset_split):
        generator = self.generate_samples(data_dir, tmp_dir, dataset_split)
        encoder = self.get_or_create_vocab(data_dir, tmp_dir)
        encoders = {'inputs': encoder, 'context': encoder}
        encoders['targets'] = self.targets_encoder
        # vocab = self.feature_encoders(data_dir)["context"]
        for sample in generator:
            sample = self.encode_example(sample, encoders)
            yield sample


    def encode_example(self, example, encoders):
        inputs = encoders['inputs'].encode(example["inputs"])
        inputs.append(text_encoder.EOS_ID)
        context = encoders['context'].encode(example["context"])
        context.append(text_encoder.EOS_ID)
        targets = encoders['targets'].encode(example["targets"])
        targets.append(text_encoder.EOS_ID)
        return {"inputs": inputs, "context": context, "targets": targets}


    def hparams(self, defaults, unused_model_hparams):
        super().hparams(defaults, unused_model_hparams)
        p = defaults
        p.modality["context"] = modalities.ModalityType.SYMBOL
        p.vocab_size["context"] = self._encoders["context"].vocab_size
        if self.packed_length:
            raise NotImplementedError("HansardLineSpeakerSpan does not "
                                      "support packed_length")


    def example_reading_spec(self):
        data_fields, data_items_to_decoders = (super().example_reading_spec())
        data_fields["context"] = tf.VarLenFeature(tf.int64)
        return (data_fields, data_items_to_decoders)


    def preprocess_example(self, example, mode, hparams):
        sep = tf.convert_to_tensor([self.CONTEXT_SEPARATOR_ID],
                                    dtype=example["inputs"].dtype)
        example["inputs"] = tf.concat([example["inputs"][:-1], sep, example["context"]], 0)
        example = super().preprocess_example(example, mode, hparams)
        return example


    def generate_samples(self, data_dir, tmp_dir, dataset_split):
        is_train = dataset_split == problem.DatasetSplit.TRAIN
        assert 'train.csv' in os.listdir(tmp_dir)
        if is_train:
            dataset_path = os.path.join(tmp_dir, 'train.csv')
        else:
            dataset_path = os.path.join(tmp_dir, 'dev.csv')
        lines = pd.read_csv(dataset_path, encoding='latin')
        lines = lines[lines.text.notnull()]
        lines['text'] = lines.text.apply(normalize_text)
        assert lines.text.isnull().sum() == 0, "Every line must have a non-null text value."
        assert (lines.text.str.len() > 0).all(), "Every line must have a text value with len > 0."
        # removes lines without a speaker name.
        # lines = lines[lines.start.notnull() | lines.end.notnull()]
        # fills in missing start/end positions.
        lines.start[lines.start.isnull() & lines.end.notnull()] = 1
        lines.end[lines.start.notnull() & lines.end.isnull()] = lines.text[lines.start.notnull() & lines.end.isnull()].apply(len)
        lines.start -= 1
        # lines.end -= 1
        has_start_end = lines.start.notnull() & lines.end.notnull()
        assert (lines.start.min() >= 0).all()
        assert lines.start[lines.end.notnull()].isnull().sum() == 0
        assert lines.end[lines.start.notnull()].isnull().sum() == 0
        assert (lines.start[has_start_end] <= lines.end[has_start_end]).all()
        assert (lines.end[has_start_end] <= lines[has_start_end].text.str.len()).all()
        lines['bio'] = lines.apply(lambda row: span2bio(s=row['text'], start=row['start'], end=row['end']), axis=1)
        if is_train and config.N_SPLIT_LINES_PASSES > 0:
            # randomly splits lines to create more training data.
            lines_split = []
            for i in range(config.N_SPLIT_LINES_PASSES):
                lines_split_temp = split_lines(lines)
                lines_split_temp['file'] += f'_{i}'
                lines_split.append(lines_split_temp)
            lines_split = pd.concat(lines_split, axis=0, ignore_index=True, sort=True)
            lines_split[['start', 'end']] = pd.DataFrame(lines_split.bio.apply(bio2span).values.tolist(), index=lines_split.index)
            # idx = np.random.randint(low=0, high=lines_split.shape[0])
            # lines_split.iloc[idx:idx+10,:]
            lines = pd.concat([lines, lines_split], axis=0, ignore_index=True, sort=True)
            lines.sort_values(by=['year', 'file', 'line'], inplace=True)
        # assigns `has_speaker` variable.
        assert (lines.bio.str.len() == lines.text.str.len()).all()
        lines['has_speaker'] = lines.bio.apply(lambda x: bool(re.search(r'[BI]', x))).astype(int)
        assert lines.has_speaker.isnull().sum() == 0
        # retrieves context surrounding each line.
        contexts = get_line_context(lines, n=config.CONTEXT_N_LINES)
        assert contexts.prev_context.isnull().sum() == 0
        assert contexts.next_context.isnull().sum() == 0
        lines = pd.merge(lines, contexts, on=['year', 'file', 'line'], how='left', validate='1:1')
        # drops lines that have a null prev_context and are not the first N lines.
        lines = lines[~((lines.prev_context.str.len() == 0) & (lines.line > config.CONTEXT_N_LINES))]
        assert (lines[lines.line == 1].prev_context.str.len() == 0).all()
        # drops lines that have a null next_context and are not an end-of-document line.
        lines = lines[~((lines.next_context.str.len() == 0) & ~lines.text.str.contains(r'<EOD>$', flags=re.IGNORECASE))]
        lines[lines.text.str.contains(r'<EOD>$', flags=re.IGNORECASE)]['next_context'] = ''
        if is_train and config.UPSAMPLE:
            weights = (1 / (lines.has_speaker.dropna().value_counts() / lines.has_speaker.dropna().shape[0])).to_dict()
            lines['class_weight'] = lines.has_speaker.apply(lambda x: weights[x])
            lines = lines.sample(lines.shape[0] * config.UPSAMPLE_FACTOR, weights=lines['class_weight'].values, replace=True)
        for _, line in lines.iterrows():
            line_text = line['text']
            prev_context = line['prev_context'] if pd.notnull(line['prev_context']) else ''
            next_context = line['next_context'] if pd.notnull(line['next_context']) else ''
            if config.RM_FLATWORLD_TAGS:
                raise NotImplementedError
            #     removes Flatworld tags from line, and then adjusts start and
            #     end accordingly.
            #     line_text2, _ = extract_flatworld_tags(line_text)
            #     if line_text2 != line_text:
            #         line_text2 = line_text2.strip()
            #         nm = line_text[line['start']:line['end']+1]
            #         assert line_text2.startswith(nm)
            #         end -= start
            #         start = 0
            #         assert nm == line_text2[start:end+1]
            #         line_text = line_text2
            context = '\n'.join([prev_context, next_context])
            targets = ' '.join(line['bio'])
            yield {'inputs': line_text, 'context': context, 'targets': targets}


@registry.register_problem
class HansardLineSpeakerSpanChar(HansardLineSpeakerSpan):
    @property
    def vocab_type(self):
        return text_problems.VocabType.CHARACTER
