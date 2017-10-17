from __future__ import print_function
import numpy as np
import tensorflow as tf
import zipfile
import codecs
import os
from some_useful_functions import (construct, create_vocabulary,
                                   get_positions_in_vocabulary, char2vec, pred2vec,
                                   char2id, id2char, flatten)


url = 'http://mattmahoney.net/dc/'

def char2batchvec(char, characters_positions_in_vocabulary):
    return np.reshape(char2vec(char, characters_positions_in_vocabulary), (1, 1, -1))


def pred2batchvec(pred):
    return np.reshape(pred2vec(pred), (1, 1, -1))


class LstmBatchGenerator(object):

    @staticmethod
    def create_vocabulary(texts):
        text = ''
        for t in texts:
            text += t
        return create_vocabulary(text)

    def __init__(self, text, batch_size, num_unrollings=1, vocabulary=None):
        self._text = text
        self._text_size = len(text)
        self._batch_size = batch_size
        self._vocabulary = vocabulary
        self._vocabulary_size = len(self._vocabulary)
        self._characters_positions_in_vocabulary = get_positions_in_vocabulary(self._vocabulary)
        self._num_unrollings = num_unrollings
        segment = self._text_size // batch_size
        self._cursor = [offset * segment for offset in range(batch_size)]
        self._last_batch = self._start_batch()

    def get_dataset_length(self):
        return len(self._text)

    def get_vocabulary_size(self):
        return self._vocabulary_size

    def _start_batch(self):
        batch = np.zeros(shape=(self._batch_size, self._vocabulary_size), dtype=np.float)
        for b in range(self._batch_size):
            batch[b, char2id('\n', self._characters_positions_in_vocabulary)] = 1.0
        return batch

    def _zero_batch(self):
        return np.zeros(shape=(self._batch_size, self._vocabulary_size), dtype=np.float)

    def _next_batch(self):
        """Generate a single batch from the current cursor position in the data."""
        batch = np.zeros(shape=(self._batch_size, self._vocabulary_size), dtype=np.float)
        for b in range(self._batch_size):
            batch[b, char2id(self._text[self._cursor[b]], self._characters_positions_in_vocabulary)] = 1.0
            self._cursor[b] = (self._cursor[b] + 1) % self._text_size
        return batch

    def char2vec(self, char):
        return np.stack(char2vec(char, self._characters_positions_in_vocabulary)), np.stack(self._zero_batch())

    def pred2vec(self, pred):
        batch = np.zeros(shape=(self._batch_size, self._vocabulary_size), dtype=np.float)
        char_id = np.argmax(pred, 1)[-1]
        batch[0, char_id] = 1.0
        return batch, self._zero_batch()

    def next(self):
        """Generate the next array of batches from the data. The array consists of
        the last batch of the previous array, followed by num_unrollings new ones.
        """
        batches = [self._last_batch]
        for step in range(self._num_unrollings):
            batches.append(self._next_batch())
        self._last_batch = batches[-1]
        return np.stack(batches[:-1]), np.concatenate(batches[1:], 0)


def characters(probabilities, vocabulary):
    """Turn a 1-hot encoding or a probability distribution over the possible
    characters back into its (most likely) character representation."""
    return [id2char(c, vocabulary) for c in np.argmax(probabilities, 1)]


def batches2string(batches, vocabulary):
    """Convert a sequence of batches back into their (most likely) string
    representation."""
    s = [u""] * batches[0].shape[0]
    for b in batches:
        s = [u"".join(x) for x in zip(s, characters(b, vocabulary))]
    return s


class Model(object):

    @classmethod
    def get_name(cls):
        return cls.name


class Lstm(Model):
    _name = 'lstm'

    @classmethod
    def check_kwargs(cls,
                     **kwargs):
        pass

    @classmethod
    def get_name(cls):
        return cls._name

    @staticmethod
    def get_special_args():
        return dict()

    @staticmethod
    def form_list_of_kwargs(kwargs_for_building, build_hyperparameters):
        output = [(construct(kwargs_for_building), dict(), list())]
        lengths = list()
        for name, values in build_hyperparameters.items():
            new_output = list()
            lengths.append(len(values))
            for base in output:
                for idx, value in enumerate(values):
                    new_base = construct(base)
                    new_base[0][name] = value
                    new_base[1][name] = value
                    new_base[2].append(idx)
                    new_output.append(new_base)
            output = new_output
        sorting_factors = [1]
        for length in reversed(lengths[1:]):
            sorting_factors.append(sorting_factors[-1] * length)
        output = sorted(output,
                        key=lambda set: sum(
                            [point_idx*sorting_factor \
                             for point_idx, sorting_factor in zip(reversed(set[2][1:]), sorting_factors)]))
        return output

    def _lstm_layer(self, inp, state, layer_idx):
        with tf.name_scope('lstm_layer_%s' % layer_idx):
            matr = self._lstm_matrices[layer_idx]
            bias = self._lstm_biases[layer_idx]
            nn = self._num_nodes[layer_idx]
            x = tf.concat([inp, state[0]], 1, name='X')
            linear_res = tf.add(tf.matmul(x, matr, name='matmul'), bias, name='linear_res')
            [sigm_arg, tanh_arg] = tf.split(linear_res, [3*nn, nn], axis=1, name='split_to_act_func_args')
            sigm_res = tf.sigmoid(sigm_arg, name='sigm_res')
            transform_vec = tf.tanh(tanh_arg, name='transformation_vector')
            [forget_gate, input_gate, output_gate] = tf.split(sigm_res, 3, axis=1, name='gates')
            new_cell_state = tf.add(forget_gate * state[1], input_gate * transform_vec, name='new_cell_state')
            new_hidden_state = tf.multiply(output_gate, tf.tanh(new_cell_state), name='new_hidden_state')
        return new_hidden_state, (new_hidden_state, new_cell_state)

    def _rnn_iter(self, embedding, all_states):
        with tf.name_scope('rnn_iter'):
            new_all_states = list()
            output = embedding
            for layer_idx, state in enumerate(all_states):
                output, state = self._lstm_layer(output, state, layer_idx)
                new_all_states.append(state)
            return output, new_all_states

    def _rnn_module(self, embeddings, all_states):
        rnn_outputs = list()
        with tf.name_scope('rnn_module'):
            for emb in embeddings:
                rnn_output, all_states = self._rnn_iter(emb, all_states)
                #print('rnn_output.shape:', rnn_output.get_shape().as_list())
                rnn_outputs.append(rnn_output)
        return rnn_outputs, all_states

    def _embed(self, inputs):
        with tf.name_scope('embeddings'):
            num_unrollings = len(inputs)
            inputs = tf.concat(inputs, 0, name='concatenated_inputs')
            embeddings = tf.matmul(inputs, self._embedding_matrix, name='embeddings_stacked')
            return tf.split(embeddings, num_unrollings, 0, name='embeddings')

    def _output_module(self, rnn_outputs):
        with tf.name_scope('output_module'):
            #print('rnn_outputs:', rnn_outputs)
            rnn_outputs = tf.concat(rnn_outputs, 0, name='concatenated_rnn_outputs')
            hs = rnn_outputs
            for layer_idx in range(self._num_output_layers):
                #print('hs.shape:', hs.get_shape().as_list())
                hs = tf.add(
                    tf.matmul(hs,
                              self._output_matrices[layer_idx],
                              name='matmul_in_%s_output_layer' % layer_idx),
                    self._output_biases[layer_idx],
                    name='res_of_%s_output_layer' % layer_idx)
                if layer_idx < self._num_output_layers - 1:
                    hs = tf.nn.relu(hs)
        return hs

    @staticmethod
    def _extract_op_name(full_name):
        scopes_stripped = full_name.split('/')[-1]
        return scopes_stripped.split(':')[0]

    def _compose_save_list(self,
                           *pairs):
        #print('start')
        with tf.name_scope('save_list'):
            save_list = list()
            for pair in pairs:
                #print('pair:', pair)
                variables = flatten(pair[0])
                #print(variables)
                new_values = flatten(pair[1])
                for variable, value in zip(variables, new_values):
                    name = self._extract_op_name(variable.name)
                    save_list.append(tf.assign(variable, value, name='assign_save_%s' % name))
            return save_list

    def _compose_reset_list(self, *args):
        with tf.name_scope('reset_list'):
            reset_list = list()
            flattened = flatten(args)
            for variable in flattened:
                shape = variable.get_shape().as_list()
                name = self._extract_op_name(variable.name)
                reset_list.append(tf.assign(variable, tf.zeros(shape), name='assign_reset_%s' % name))
            return reset_list

    def _compute_lstm_matrix_parameters(self, idx):
        if idx == 0:
            print(self._num_nodes)
            input_dim = self._num_nodes[0] + self._embedding_size
        else:
            input_dim = self._num_nodes[idx-1] + self._num_nodes[idx]
        output_dim = 4 * self._num_nodes[idx]
        stddev = self._init_parameter * np.sqrt(1./input_dim)
        return input_dim, output_dim, stddev

    def _compute_output_matrix_parameters(self, idx):
        if idx == 0:
            #print('self._num_nodes:', self._num_nodes)
            input_dim = self._num_nodes[-1]
        else:
            input_dim = self._num_output_nodes[idx-1]
        if idx == self._num_output_layers - 1:
            output_dim = self._vocabulary_size
        else:
            output_dim = self._num_output_nodes[idx]
        stddev = self._init_parameter * np.sqrt(1. / input_dim)
        return input_dim, output_dim, stddev

    def _l2_loss(self, matrices):
        with tf.name_scope('l2_loss'):
            regularizer = tf.contrib.layers.l2_regularizer(.5)
            loss = 0
            for matr in matrices:
                shape = matr.get_shape().as_list()
                divider = 1
                for dim in shape:
                    divider *= dim
                divider = float(divider)
                loss += regularizer(matr) / divider
            return loss

    def __init__(self,
                 batch_size=64,
                 num_layers=2,
                 num_nodes=[112, 113],
                 num_output_layers=1,
                 num_output_nodes=[],
                 vocabulary_size=None,
                 embedding_size=128,
                 num_unrollings=10,
                 init_parameter=.3):
        self._batch_size = batch_size
        self._num_layers = num_layers
        self._num_nodes = num_nodes
        self._vocabulary_size = vocabulary_size
        self._embedding_size = embedding_size
        self._num_output_layers = num_output_layers
        self._num_output_nodes = num_output_nodes
        self._num_unrollings = num_unrollings
        self._init_parameter = init_parameter

        self._embedding_matrix = tf.Variable(
            tf.truncated_normal([self._vocabulary_size, self._embedding_size],
                                stddev=self._init_parameter*np.sqrt(1./self._vocabulary_size)),
            name='embedding_matrix')

        self._lstm_matrices = list()
        self._lstm_biases = list()
        for layer_idx in range(self._num_layers):
            input_dim, output_dim, stddev = self._compute_lstm_matrix_parameters(layer_idx)
            self._lstm_matrices.append(tf.Variable(tf.truncated_normal([input_dim,
                                                                        output_dim],
                                                                       stddev=stddev),
                                                   name='lstm_matrix_%s' % layer_idx))
            self._lstm_biases.append(tf.Variable(tf.zeros([output_dim]), name='lstm_bias_%s' % layer_idx))

        self._output_matrices = list()
        self._output_biases = list()
        for layer_idx in range(self._num_output_layers):
            input_dim, output_dim, stddev = self._compute_output_matrix_parameters(layer_idx)
            #print('input_dim:', input_dim)
            #print('output_dim:', output_dim)
            self._output_matrices.append(tf.Variable(tf.truncated_normal([input_dim, output_dim],
                                                                         stddev=stddev),
                                                     name='output_matrix_%s' % layer_idx))
            self._output_biases.append(tf.Variable(tf.zeros([output_dim])))

        with tf.name_scope('train'):
            saved_states = list()
            for layer_idx, layer_num_nodes in enumerate(self._num_nodes):
                saved_states.append(
                    (tf.Variable(
                         tf.zeros([self._batch_size, layer_num_nodes]),
                         trainable=False,
                         name='saved_state_%s_%s' % (layer_idx, 0)),
                     tf.Variable(
                         tf.zeros([self._batch_size, layer_num_nodes]),
                         trainable=False,
                         name='saved_state_%s_%s' % (layer_idx, 1)))
                )

            self.inputs = tf.placeholder(tf.float32,
                                         shape=[self._num_unrollings, self._batch_size, self._vocabulary_size])
            self.labels = tf.placeholder(tf.float32,
                                         shape=[self._num_unrollings * self._batch_size, self._vocabulary_size])

            inputs = tf.unstack(self.inputs)

            all_states = saved_states
            embeddings = self._embed(inputs)
            rnn_outputs, all_states = self._rnn_module(embeddings, all_states)
            logits = self._output_module(rnn_outputs)

            save_ops = self._compose_save_list((saved_states, all_states))
            with tf.control_dependencies(save_ops):
                l2_loss = self._l2_loss(self._output_matrices[:-1])
                self.loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels=self.labels, logits=logits))
                self.learning_rate = tf.placeholder(tf.float32, name='learning_rate')
                optimizer = tf.train.GradientDescentOptimizer(self.learning_rate)
                #optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
                gradients, v = zip(*optimizer.compute_gradients(self.loss + l2_loss))
                gradients, _ = tf.clip_by_global_norm(gradients, 1.)
                self.train_op = optimizer.apply_gradients(zip(gradients, v))
                self.predictions = tf.nn.softmax(logits)
        with tf.name_scope('validation'):
            self.sample_input = tf.placeholder(tf.float32,
                                               shape=[1, 1, self._vocabulary_size],
                                               name='sample_input')
            sample_input = tf.reshape(self.sample_input, [1, -1])
            saved_sample_state = list()
            for layer_idx, layer_num_nodes in enumerate(self._num_nodes):
                saved_sample_state.append(
                    (tf.Variable(
                        tf.zeros([1, layer_num_nodes]),
                        trainable=False,
                        name='saved_sample_state_%s_%s' % (layer_idx, 0)),
                     tf.Variable(
                         tf.zeros([1, layer_num_nodes]),
                         trainable=False,
                         name='saved_sample_state_%s_%s' % (layer_idx, 1)))
                )
            reset_list = self._compose_reset_list(saved_sample_state)
            self.reset_sample_state = tf.group(*reset_list)

            embeddings = self._embed([sample_input])
            #print('embeddings:', embeddings)
            rnn_output, sample_state = self._rnn_module(embeddings, saved_sample_state)
            sample_logits = self._output_module(rnn_output)

            sample_save_ops = self._compose_save_list((saved_sample_state, sample_state))

            with tf.control_dependencies(sample_save_ops):
                self.sample_prediction = tf.nn.softmax(sample_logits)
        self.saver = tf.train.Saver(max_to_keep=None)

    def get_default_hooks(self):
        hooks = dict()
        hooks['inputs'] = self.inputs
        hooks['labels'] = self.labels
        hooks['train_op'] = self.train_op
        hooks['learning_rate'] = self.learning_rate
        hooks['loss'] = self.loss
        hooks['predictions'] = self.predictions
        hooks['validation_inputs'] = self.sample_input
        hooks['validation_predictions'] = self.sample_prediction
        hooks['reset_validation_state'] = self.reset_sample_state
        hooks['saver'] = self.saver
        return hooks

    def get_building_parameters(self):
        pass