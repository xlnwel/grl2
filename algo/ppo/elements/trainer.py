from functools import partial
import os
import numpy as np
import jax
from jax import random
import jax.numpy as jnp
import haiku as hk

from core.ckpt.pickle import save, restore
from tools.log import do_logging
from core.names import TRAIN_AXIS
from core.elements.trainer import TrainerBase, create_trainer
from core import optimizer
from core.typing import AttrDict, dict2AttrDict
from tools.display import print_dict_info
from tools.rms import RunningMeanStd
from tools.utils import flatten_dict, prefix_name, yield_from_tree_with_indices


def construct_fake_data(env_stats, aid):
  b = 8
  s = 400
  u = len(env_stats.aid2uids[aid])
  shapes = env_stats.obs_shape[aid]
  dtypes = env_stats.obs_dtype[aid]
  action_dim = env_stats.action_dim[aid]
  basic_shape = (b, s, u)
  data = {k: jnp.zeros((b, s+1, u, *v), dtypes[k]) 
    for k, v in shapes.items()}
  data = dict2AttrDict(data)
  data.setdefault('global_state', data.obs)
  data.action = jnp.zeros((*basic_shape, action_dim), jnp.float32)
  data.value = jnp.zeros(basic_shape, jnp.float32)
  data.reward = jnp.zeros(basic_shape, jnp.float32)
  data.discount = jnp.zeros(basic_shape, jnp.float32)
  data.reset = jnp.zeros(basic_shape, jnp.float32)
  data.mu_logprob = jnp.zeros(basic_shape, jnp.float32)
  data.mu_logits = jnp.zeros((*basic_shape, action_dim), jnp.float32)
  data.advantage = jnp.zeros(basic_shape, jnp.float32)
  data.v_target = jnp.zeros(basic_shape, jnp.float32)

  print_dict_info(data)
  
  return data


class Trainer(TrainerBase):
  def add_attributes(self):
    self.popart = RunningMeanStd((0, 1), name='popart', ndim=1)
    self.indices = np.arange(self.config.n_runners * self.config.n_envs)

  def build_optimizers(self):
    theta = self.model.theta.copy()
    if self.config.get('theta_opt'):
      self.opts.theta, self.params.theta = optimizer.build_optimizer(
        params=theta, 
        **self.config.theta_opt, 
        name='theta'
      )
    else:
      self.params.theta = AttrDict()
      self.opts.policy, self.params.theta.policy = optimizer.build_optimizer(
        params=theta.policy, 
        **self.config.policy_opt, 
        name='policy'
      )
      self.opts.value, self.params.theta.value = optimizer.build_optimizer(
        params=theta.value, 
        **self.config.value_opt, 
        name='value'
      )

  def compile_train(self):
    _jit_train = jax.jit(self.theta_train, static_argnames=['debug'])
    def jit_train(*args, **kwargs):
      self.rng, rng = random.split(self.rng)
      return _jit_train(*args, rng=rng, **kwargs)
    self.jit_train = jit_train

    self.haiku_tabulate()

  def train(self, data: AttrDict):
    if self.config.n_runners * self.config.n_envs < self.config.n_mbs:
      self.indices = np.arange(self.config.n_mbs)
      data = jax.tree_util.tree_map(
        lambda x: jnp.reshape(x, (self.config.n_mbs, -1, *x.shape[2:])), data)
    theta = self.model.theta.copy()
    all_stats = AttrDict()
    for e in range(self.config.n_epochs):
      np.random.shuffle(self.indices)
      _indices = np.split(self.indices, self.config.n_mbs)
      v_target = []
      for i, d in enumerate(yield_from_tree_with_indices(
          data, _indices, axis=TRAIN_AXIS.BATCH)):
        if self.config.popart:
          d.popart_mean = self.popart.mean
          d.popart_std = self.popart.std
        theta, self.params.theta, stats = \
          self.jit_train(
            theta, 
            opt_state=self.params.theta, 
            data=d, 
            debug=self.config.debug
          )
        v_target.append(stats.raw_v_target)
        # print_dict_info(stats)
        if e == 0 and i == 0:
          all_stats.update(**prefix_name(stats, name=f'group_first_epoch'))
        elif e == self.config.n_epochs-1 and i == self.config.n_mbs - 1:
          all_stats.update(**prefix_name(stats, name=f'group_last_epoch'))
    self.model.set_weights(theta)

    if self.config.popart:
      v_target = np.concatenate(v_target)
      self.popart.update(v_target)
      all_stats['theta/popart/mean'] = self.popart.mean
      all_stats['theta/popart/std'] = self.popart.std

    data = flatten_dict({k: v 
      for k, v in data.items() if v is not None}, prefix='data')
    all_stats.update(data)

    # for v in theta.values():
    #   all_stats.update(flatten_dict(
    #     jax.tree_util.tree_map(np.linalg.norm, v)))

    return self.config.n_epochs * self.config.n_mbs, all_stats

  def theta_train(
    self, 
    theta, 
    rng, 
    opt_state, 
    data, 
    debug=True
  ):
    do_logging('train is traced', level='info')
    rngs = random.split(rng, 3)
    if self.config.get('theta_opt'):
      theta, opt_state, stats = optimizer.optimize(
        self.loss.loss, 
        theta, 
        opt_state, 
        kwargs={
          'rng': rngs[0], 
          'data': data, 
        }, 
        opt=self.opts.theta, 
        name='opt/theta', 
        debug=debug
      )
    else:
      theta.value, opt_state.value, stats = optimizer.optimize(
        self.loss.value_loss, 
        theta.value, 
        opt_state.value, 
        kwargs={
          'rng': rngs[0], 
          'policy_theta': theta.policy, 
          'data': data, 
        }, 
        opt=self.opts.value, 
        name='opt/value', 
        debug=debug
      )
      theta.policy, opt_state.policy, stats = optimizer.optimize(
        self.loss.policy_loss, 
        theta.policy, 
        opt_state.policy, 
        kwargs={
          'rng': rngs[1], 
          'data': data, 
          'stats': stats
        }, 
        opt=self.opts.policy, 
        name='opt/policy', 
        debug=debug
      )

    return theta, opt_state, stats

  def save_optimizer(self):
    super().save_optimizer()
    self.save_popart()
  
  def restore_optimizer(self):
    super().restore_optimizer()
    self.restore_popart()

  def save(self):
    super().save()
    self.save_popart()
  
  def restore(self):
    super().restore()
    self.restore_popart()

  def get_popart_dir(self):
    path = os.path.join(self.config.root_dir, self.config.model_name)
    return path

  def save_popart(self):
    filedir = self.get_popart_dir()
    save(self.popart, filedir=filedir, filename='popart', name='popart')

  def restore_popart(self):
    filedir = self.get_popart_dir()
    self.popart = restore(
      filedir=filedir, filename='popart', 
      default=self.popart, 
      name='popart'
    )

  def get_optimizer_weights(self):
    weights = super().get_optimizer_weights()
    weights['popart'] = self.popart.get_rms_stats()
    return weights

  def set_optimizer_weights(self, weights):
    popart_stats = weights.pop('popart')
    self.popart.set_rms_stats(*popart_stats)
    super().set_optimizer_weights(weights)

  # def haiku_tabulate(self, data=None):
  #   rng = random.PRNGKey(0)
  #   if data is None:
  #     data = construct_fake_data(self.env_stats, 0)
  #   theta = self.model.theta.copy()
  #   print(hk.experimental.tabulate(self.theta_train)(
  #     theta, rng, self.params.theta, data
  #   ))
  #   breakpoint()


create_trainer = partial(create_trainer, name='ppo', trainer_cls=Trainer)


if __name__ == '__main__':
  import haiku as hk
  from tools.yaml_op import load_config
  from envs.func import create_env
  from .model import create_model
  from .loss import create_loss
  from tools.log import pwc
  config = load_config('algo/ppo/configs/magw_a2c')
  config = load_config('distributed/sync/configs/smac')
  
  env = create_env(config.env)
  model = create_model(config.model, env.stats())
  loss = create_loss(config.loss, model)
  trainer = create_trainer(config.trainer, env.stats(), loss)
  data = construct_fake_data(env.stats(), 0)
  rng = random.PRNGKey(0)
  pwc(hk.experimental.tabulate(trainer.jit_train)(
    model.theta, rng, trainer.params.theta, data), color='yellow')
  # data = construct_fake_data(env.stats(), 0, True)
  # pwc(hk.experimental.tabulate(trainer.raw_meta_train)(
  #   model.eta, model.theta, trainer.params, data), color='yellow')
