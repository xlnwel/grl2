import os
import numpy as np
import logging
import collections
import jax
from jax import lax, nn, random
import jax.numpy as jnp
import haiku as hk

from core.log import do_logging
from core.elements.model import Model as ModelBase
from core.typing import dict2AttrDict, AttrDict
from tools.file import source_file
from jax_tools import jax_dist
from env.typing import EnvOutput

# register ppo-related networks 
source_file(os.path.realpath(__file__).replace('model.py', 'nn.py'))
logger = logging.getLogger(__name__)


def construct_fake_data(env_stats, aid):
    basic_shape = (1, 1, env_stats.n_units)
    shapes = env_stats.obs_shape[aid]
    dtypes = env_stats.obs_dtype[aid]
    data = {k: jnp.zeros((*basic_shape, *v), dtypes[k]) 
        for k, v in shapes.items()}
    data = dict2AttrDict(data)
    if env_stats.is_action_discrete[aid]:
        data.action = jnp.zeros(basic_shape, jnp.float32)
    else:
        action_dim = env_stats.action_dim[aid]
        data.action = jnp.zeros((*basic_shape, action_dim), jnp.float32)

    return data


class Model(ModelBase):
    def add_attributes(self):
        self.elite_indices = None
        self.elite_idx = None
        self.n_selected_elites = collections.defaultdict(lambda: 0)

    def build_nets(self):
        aid = self.config.get('aid', 0)
        self.is_action_discrete = self.env_stats.is_action_discrete[aid]
        data = construct_fake_data(self.env_stats, aid)

        self.params.model, self.modules.model = self.build_net(
            data.obs[:, 0, :], data.action[:, 0], name='model')
        self.params.emodels, self.modules.emodels = self.build_net(
            data.obs, data.action, name='emodels')
        self.params.reward, self.modules.reward = self.build_net(
            data.obs, data.action, name='reward')

    @property
    def theta(self):
        return self.params.subdict('emodels', 'reward')

    def rank_elites(self, elite_indices):
        self.elite_indices = elite_indices

    def choose_elite(self, idx=None):
        if idx is None:
            idx = np.random.randint(self.config.n_elites)
        self.elite_idx = self.elite_indices[idx]
        self.n_selected_elites[f'elite{self.elite_idx}'] += 1
        model = {f'model/mlp/{k.split("/")[-1]}': v 
            for k, v in self.params.emodels.items() 
            if k.startswith(f'emodels/model{self.elite_idx}')
        }
        assert set(model) == set(self.params.model)
        self.params.model = model
        return self.elite_idx
    
    def evolve_model(self, score):
        indices = np.argsort(score)
        model = {k.replace(f'model{indices[-1]}', f'model{indices[0]}'): v
            for k, v in self.params.emodels.items() 
            if k.startswith(f'emodels/model{indices[-1]}')
        }
        for k, v in model.items():
            init = nn.initializers.normal(self.config.rn_std, dtype=jnp.float32)
            self.act_rng, rng = random.split(self.act_rng)
            model[k] = v + init(rng, v.shape)
        keys = set(self.params.emodels)
        self.params.emodels.update(model)
        assert keys == set(self.params.emodels), (keys, set(self.params.emodels))

    def get_ith_model(self, i):
        model = {f'model/mlp/{k.split("/")[-1]}': v 
            for k, v in self.params.emodels.items() 
            if k.startswith(f'emodels/model{i}')
        }
        return model

    def action(self, data, evaluation):
        self.act_rng, act_rng = jax.random.split(self.act_rng) 
        env_outs, stats, state = self.jit_action(
            self.params, act_rng, data, evaluation)
        stats.update(self.n_selected_elites)
        return env_outs, stats, state

    def raw_action(
        self, 
        params, 
        rng, 
        data, 
        evaluation=False, 
    ):
        rngs = random.split(rng, 2)
        next_obs, stats = self.next_obs(
            params.model, rngs[0], data.obs, data.action, evaluation)
        reward = self.reward(params.reward, rngs[1], data.obs, data.action)
        discount = jnp.ones_like(reward, dtype=jnp.float32)
        reset = jnp.zeros_like(reward, dtype=jnp.float32)
        obs = {'obs': next_obs, 'global_state': next_obs}
        env_out = EnvOutput(obs, reward, discount, reset)

        return env_out, stats, data.state

    def next_obs(self, params, rng, obs, action, evaluation):
        rngs = random.split(rng, 2)
        dist = self.modules.model(params, rngs[0], obs, action)
        next_obs = dist.mode() if evaluation else dist.sample(seed=rngs[1])
        
        stats = dict2AttrDict(dist.get_stats('model'))

        return next_obs, stats

    def reward(self, params, rng, obs, action):
        dist = self.modules.reward(params, rng, obs, action)
        rewards = dist.mode()
        if isinstance(dist, jax_dist.MultivariateNormalDiag):
            rewards = jnp.squeeze(rewards, -1)

        return rewards


def setup_config_from_envstats(config, env_stats):
    if 'aid' in config:
        aid = config['aid']
        config.emodels.out_size = env_stats.obs_shape[aid]['obs'][0]
    else:
        config.emodels.out_size = env_stats.obs_shape['obs'][0]
    config.model = config.emodels.copy()
    config.model.nn_id = 'model'
    config.model.pop('n')

    return config


def create_model(
    config, 
    env_stats, 
    name='model', 
    **kwargs
): 
    config = setup_config_from_envstats(config, env_stats)

    return Model(
        config=config, 
        env_stats=env_stats, 
        name=name,
        **kwargs
    )
