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

"""

The hyperparameters used in the model:
- learning_rate - the initial value of the learning rate
- max_grad_norm - the maximum permissible norm of the gradient
- num_layers - the number of LSTM layers
- songlength - the number of unrolled steps of LSTM
- hidden_size - the number of LSTM units
- max_epoch - the number of epochs trained with the initial learning rate
- max_max_epoch - the total number of epochs for training
- keep_prob - the probability of keeping weights in the dropout layer
- lr_decay - the decay of the learning rate for each epoch after "max_epoch"
- batch_size - the batch size

The hyperparameters that could be used in the model:
- init_scale - the initial scale of the weights

To run:

$ python rnn_gan.py --model small|medium|large --datadir simple-examples/data/ --traindir dir-for-checkpoints-and-plots --select_validation_percentage 0-40 --select_test_percentage 0-40

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time, os, sys

import numpy as np
import tensorflow as tf
from subprocess import call, Popen

from music_data_utils import MusicDataLoader

flags = tf.flags
logging = tf.logging

flags.DEFINE_string(
    "model", "medium",
    "A type of model. Possible options are: small, medium, large.")
flags.DEFINE_string("datadir", None, "Directory to save and load midi music files.")
flags.DEFINE_string("traindir", None, "Directory to save checkpoints and gnuplot files.")
flags.DEFINE_integer("epochs_per_checkpoint", 10,
                     "How many training epochs to do per checkpoint.")
flags.DEFINE_string("call_after", None, "Call this command after exit.")
flags.DEFINE_integer("exit_after", 200,
                     "exit after this many minutes")
flags.DEFINE_integer("select_validation_percentage", None,
                     "Select random percentage of data as validation set.")
flags.DEFINE_integer("select_test_percentage", None,
                     "Select random percentage of data as test set.")
FLAGS = flags.FLAGS


def data_type():
  #return tf.float16 if FLAGS.use_fp16 else tf.float32
  return tf.float32

def linear(inp, output_dim, scope=None, stddev=1.0, reuse_scope=False):
  norm = tf.random_normal_initializer(stddev=stddev)
  const = tf.constant_initializer(0.0)
  with tf.variable_scope(scope or 'linear') as scope:
    if reuse_scope:
      scope.reuse_variables()
    w = tf.get_variable('w', [inp.get_shape()[1], output_dim], initializer=norm)
    b = tf.get_variable('b', [output_dim], initializer=const)
  return tf.matmul(inp, w) + b

class RNNGAN(object):
  """The RNNGAN model."""

  def __init__(self, is_training, config, numfeatures=None):
    self.batch_size = batch_size = config.batch_size
    self.songlength = songlength = config.songlength
    size = config.hidden_size
    self.global_step            = tf.Variable(0, trainable=False)

    self._input_data = tf.placeholder(tf.float32, [batch_size, songlength, numfeatures])

    with tf.variable_scope('G') as scope:
      lstm_cell = tf.nn.rnn_cell.BasicLSTMCell(size, forget_bias=1.0, state_is_tuple=True)
      if is_training and config.keep_prob < 1:
        lstm_cell = tf.nn.rnn_cell.DropoutWrapper(
            lstm_cell, output_keep_prob=config.keep_prob)
      cell = tf.nn.rnn_cell.MultiRNNCell([lstm_cell] * config.num_layers, state_is_tuple=True)

      self._initial_state = cell.zero_state(batch_size, data_type())

      inputs = tf.random_uniform(shape=[batch_size, songlength, numfeatures], minval=0.0, maxval=1.0)

      # Make list of tensors. One per step in recurrence.
      # Each tensor is batchsize*numfeatures.
      inputs = [tf.squeeze(input_, [1])
                for input_ in tf.split(1, songlength, inputs)]
      transformed = [tf.nn.relu(linear(inp, size, scope='input_layer', reuse_scope=(i!=0))) for i,inp in enumerate(inputs)]

      outputs, state = tf.nn.rnn(cell, transformed, initial_state=self._initial_state)

      #lengths_freqs_velocities = []
      #for i,output in enumerate(outputs):
      #  length_freq_velocity = tf.nn.relu(linear(output, numfeatures, scope='output_layer', reuse_scope=(i!=0)))
      #  lengths_freqs_velocities.append(length_freq_velocity)
      lengths_freqs_velocities = [tf.nn.relu(linear(output, numfeatures, scope='output_layer', reuse_scope=(i!=0))) for i,output in enumerate(outputs)]
   
    self._final_state = state

    # The discriminator tries to tell the difference between samples from the
    # true data distribution (self.x) and the generated samples (self.z).
    #
    # Here we create two copies of the discriminator network (that share parameters),
    # as you cannot use the same network with different inputs in TensorFlow.
    with tf.variable_scope('D') as scope:
      # Make list of tensors. One per step in recurrence.
      # Each tensor is batchsize*numfeatures.
      data_inputs = [tf.squeeze(input_, [1])
                for input_ in tf.split(1, songlength, self._input_data)]
      if data_inputs[0].get_shape() != lengths_freqs_velocities[0].get_shape():
        print('data inputs shape {} != generated data shape {}'.format(data_inputs[0].get_shape(), lengths_freqs_velocities[0].get_shape()))
      self.discriminator_for_real_data = self.discriminator(data_inputs, config, is_training)
      scope.reuse_variables()
      self.discriminator_for_generated_data = self.discriminator(lengths_freqs_velocities, config, is_training)

    # Define the loss for discriminator and generator networks (see the original
    # paper for details), and create optimizers for both
    self.d_loss = tf.reduce_mean(-tf.log(tf.clip_by_value(self.discriminator_for_real_data, 1e-1000, 1.0)) - tf.log(1 - tf.clip_by_value(self.discriminator_for_generated_data, 0.0, 1.0-1e-1000)))
    self.g_loss = tf.reduce_mean(-tf.log(tf.clip_by_value(self.discriminator_for_generated_data, 1e-1000, 1.0)))

    vs = tf.trainable_variables()
    self.d_params = [v for v in vs if v.name.startswith('model/D/')]
    self.g_params = [v for v in vs if v.name.startswith('model/G/')]

    if not is_training:
      return

    self._lr = tf.Variable(0.0, trainable=False)

    d_optimizer = tf.train.GradientDescentOptimizer(self._lr*config.d_lr_factor)
    d_grads, _ = tf.clip_by_global_norm(tf.gradients(self.d_loss, self.d_params),
                                        config.max_grad_norm)
    self.opt_d = d_optimizer.apply_gradients(zip(d_grads, self.d_params))
    g_optimizer = tf.train.GradientDescentOptimizer(self._lr)
    g_grads, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, self.g_params),
                                        config.max_grad_norm)
    self.opt_g = g_optimizer.apply_gradients(zip(g_grads, self.g_params))

    self._new_lr = tf.placeholder(tf.float32, shape=[], name="new_learning_rate")
    self._lr_update = tf.assign(self._lr, self._new_lr)

  def discriminator(self, inputs, config, is_training):
    lstm_cell = tf.nn.rnn_cell.BasicLSTMCell(config.hidden_size, forget_bias=1.0, state_is_tuple=True)
    if is_training and config.keep_prob < 1:
      lstm_cell = tf.nn.rnn_cell.DropoutWrapper(
          lstm_cell, output_keep_prob=config.keep_prob)
    cell = tf.nn.rnn_cell.MultiRNNCell([lstm_cell] * config.num_layers, state_is_tuple=True)
    self._initial_state = cell.zero_state(config.batch_size, data_type())
    if is_training and config.keep_prob < 1:
      inputs = [tf.nn.dropout(inp, config.keep_prob) for inp in inputs]
    outputs, state = tf.nn.rnn(cell, inputs, initial_state=self._initial_state)

    # decision = tf.sigmoid(linear(outputs[-1], 1, 'decision'))
    decisions = [tf.sigmoid(linear(output, 1, 'decision', reuse_scope=(i!=0))) for i,output in enumerate(outputs)]
    decision = tf.reduce_mean(tf.pack(decisions))
    return decision

  
  def assign_lr(self, session, lr_value):
    session.run(self._lr_update, feed_dict={self._new_lr: lr_value})

  @property
  def input_data(self):
    return self._input_data

  @property
  def targets(self):
    return self._targets

  @property
  def initial_state(self):
    return self._initial_state

  @property
  def cost(self):
    return self._cost

  @property
  def final_state(self):
    return self._final_state

  @property
  def lr(self):
    return self._lr

  @property
  def train_op(self):
    return self._train_op


class SmallConfig(object):
  """Small config."""
  init_scale = 0.1
  learning_rate = 1.0
  d_lr_factor = 0.5
  max_grad_norm = 5
  num_layers = 2
  songlength = 200
  hidden_size = 200
  max_epoch = 4
  max_max_epoch = 13
  keep_prob = 1.0
  lr_decay = 0.9
  batch_size = 10
  vocab_size = 10000


class MediumConfig(object):
  """Medium config."""
  init_scale = 0.05
  learning_rate = 1.0
  d_lr_factor = 0.5
  max_grad_norm = 5
  num_layers = 2
  songlength = 350
  hidden_size = 650
  max_epoch = 6
  max_max_epoch = 39
  keep_prob = 0.5
  lr_decay = 0.9
  batch_size = 20
  vocab_size = 10000


class LargeConfig(object):
  """Large config."""
  init_scale = 0.04
  learning_rate = 1.0
  d_lr_factor = 0.5
  max_grad_norm = 10
  num_layers = 2
  songlength = 500
  hidden_size = 1500
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 0.9
  batch_size = 20
  vocab_size = 10000


class TestConfig(object):
  """Tiny config, for testing."""
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 1
  num_layers = 1
  songlength = 2
  hidden_size = 2
  max_epoch = 1
  max_max_epoch = 1
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000


def run_epoch(session, model, loader, datasetlabel, eval_op1, eval_op2, verbose=False):
  """Runs the model on the given data."""
  #epoch_size = ((len(data) // model.batch_size) - 1) // model.songlength
  start_time = time.time()
  g_losses, d_losses = 0.0, 0.0
  iters = 0
  state = session.run(model.initial_state)
  loader.rewind(part=datasetlabel)
  batch = loader.get_batch(model.batch_size, model.songlength, part=datasetlabel)
  while batch is not None:
    #fetches = [model.cost, model.final_state, eval_op]
    fetches = [model.g_loss, model.d_loss, eval_op1, eval_op2]
    feed_dict = {}
    feed_dict[model.input_data] = batch
    #for i, (c, h) in enumerate(model.initial_state):
    #  feed_dict[c] = state[i].c
    #  feed_dict[h] = state[i].h
    #cost, state, _ = session.run(fetches, feed_dict)
    g_loss, d_loss, _, _ = session.run(fetches, feed_dict)
    g_losses += g_loss
    d_losses += d_loss
    iters += 1

    #if verbose and iters-1 % 100 == 99:
    print("%d batch loss: G: %.3f, D: %.3f, avg loss: G: %.3f, D: %.3f speed: %.1f songs per sec" %
            (iters, g_loss, d_loss, g_losses/iters, d_losses/iters,
             float(iters * model.batch_size)/(time.time() - start_time)))
    batch = loader.get_batch(model.batch_size, model.songlength, part=datasetlabel)

  if iters == 0:
    return (None,None)

  return (g_losses/iters, d_losses/iters)

def get_config():
  if FLAGS.model == "small":
    return SmallConfig()
  elif FLAGS.model == "medium":
    return MediumConfig()
  elif FLAGS.model == "large":
    return LargeConfig()
  elif FLAGS.model == "test":
    return TestConfig()
  else:
    raise ValueError("Invalid model: %s", FLAGS.model)


def main(_):
  if not FLAGS.datadir:
    raise ValueError("Must set --datadir to midi music dir.")
  if not FLAGS.traindir:
    raise ValueError("Must set --traindir to dir where I can save model and plots.")
  
  loader = MusicDataLoader(FLAGS.datadir, FLAGS.select_validation_percentage, FLAGS.select_test_percentage)
  numfeatures = loader.get_numfeatures()
  print('num features:{}'.format(numfeatures))

  config = get_config()
  eval_config = get_config()
  eval_config.batch_size = 1
  #eval_config.songlength = 500

  train_start_time = time.time()

  with tf.Graph().as_default(), tf.Session() as session:
    with tf.variable_scope("model", reuse=None):
      m = RNNGAN(is_training=True, config=config, numfeatures=numfeatures)
    with tf.variable_scope("model", reuse=True):
      mvalid = RNNGAN(is_training=False, config=eval_config, numfeatures=numfeatures)
      mtest = RNNGAN(is_training=False, config=eval_config, numfeatures=numfeatures)

    saver = tf.train.Saver(tf.all_variables())
    ckpt = tf.train.get_checkpoint_state(FLAGS.traindir)
    if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
      print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
      saver.restore(session, ckpt.model_checkpoint_path)
    else:
      print("Created model with fresh parameters.")
      session.run(tf.initialize_all_variables())

    summaries_dir = None
    plots_dir = None
    if FLAGS.traindir:
      summaries_dir = os.path.join(FLAGS.traindir, 'summaries')
      plots_dir = os.path.join(FLAGS.traindir, 'plots')
      try: os.makedirs(FLAGS.traindir)
      except: pass
      try: os.makedirs(summaries_dir)
      except: pass
      try: os.makedirs(plots_dir)
      except: pass

    for i in range(config.max_max_epoch):
      lr_decay = config.lr_decay ** max(i - config.max_epoch, 0.0)
      m.assign_lr(session, config.learning_rate * lr_decay)

      print("Epoch: %d Learning rate: %.3f" % (i + 1, session.run(m.lr)))
      train_g_loss,train_d_loss = run_epoch(session, m, loader, 'train', m.opt_d, m.opt_g, verbose=True)
      print("Epoch: %d Train loss: G: %.3f, D: %.3f" % (i + 1, train_g_loss, train_d_loss))
      valid_g_loss,valid_d_loss = run_epoch(session, mvalid, loader, 'validation', tf.no_op(), tf.no_op())
      print("Epoch: %d Valid loss: G: %.3f, D: %.3f" % (i + 1, valid_g_loss, valid_d_loss))
      
      if i % FLAGS.epochs_per_checkpoint == 0:
        checkpoint_path = os.path.join(FLAGS.traindir, "translate.ckpt")
        saver.save(session, checkpoint_path, global_step=m.global_step)
        step_time, loss = 0.0, 0.0
        if plots_dir:
          if not os.path.exists(os.path.join(plots_dir, 'gnuplot-input.txt')):
            with open(os.path.join(plots_dir, 'gnuplot-input.txt'), 'w') as f:
              f.write('# global-step learning-rate train-g-loss train-d-loss valid-g-loss valid-d-loss\n')
          with open(os.path.join(plots_dir, 'gnuplot-input.txt'), 'a') as f:
            f.write('%d %.4f %.2f %.2f %.3f %.3f\n'%(m.global_step.eval(), m.lr.eval(), train_g_loss, train_d_loss, valid_g_loss, valid_d_loss))
          if not os.path.exists(os.path.join(plots_dir, 'gnuplot-commands.txt')):
            with open(os.path.join(plots_dir, 'gnuplot-commands.txt'), 'a') as f:
              f.write('set terminal postscript eps color butt "Times" 14\nset yrange [0:400]\nset output "loss.eps"\nplot \'gnuplot-input.txt\' using ($1):($3) title \'train G\' with linespoints, \'gnuplot-input.txt\' using ($1):($4) title \'train D\' with linespoints, \'gnuplot-input.txt\' using ($1):($5) title \'valid G\' with linespoints, \'gnuplot-input.txt\' using ($1):($6) title \'valid D\' with linespoints, \n')
          Popen(['gnuplot','gnuplot-commands.txt'], cwd=plots_dir)
        if FLAGS.exit_after > 0 and time.time() - train_start_time > FLAGS.exit_after*60:
          print("%s: Has been running for %d seconds. Will exit (exiting after %d minutes)."%(datetime.datetime.today().strftime('%Y-%m-%d %H:%M:%S'), (int)(time.time() - start_time), FLAGS.exit_after))
          if FLAGS.call_after is not None:
            print("%s: Will call \"%s\" before exiting."%(datetime.datetime.today().strftime('%Y-%m-%d %H:%M:%S'), FLAGS.call_after))
            res = call(FLAGS.call_after.split(" "))
            print ('{}: call returned {}.'.format(datetime.datetime.today().strftime('%Y-%m-%d %H:%M:%S'), res))
          exit()
      sys.stdout.flush()

    test_g_loss,test_d_loss = run_epoch(session, mtest, loader, 'test', tf.no_op(), tf.no_op())
    print("Test loss G: %.3f, D: %.3f" %(test_g_loss, test_d_loss))


if __name__ == "__main__":
  tf.app.run()
