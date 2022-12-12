import os
import math
import logging
import jax
from jax import lax, nn, random
import jax.numpy as jnp
import haiku as hk
import chex

from core.log import do_logging
from core.elements.model import Model as ModelBase
from core.typing import dict2AttrDict
from tools.file import source_file
from jax_tools import jax_dist
from .utils import get_hx

# register ppo-related networks 
source_file(os.path.realpath(__file__).replace('model.py', 'nn.py'))
logger = logging.getLogger(__name__)


def construct_fake_data(env_stats, aid):
    basic_shape = (1, len(env_stats.aid2uids[aid]))
    shapes = env_stats.obs_shape[aid]
    dtypes = env_stats.obs_dtype[aid]
    action_dim = env_stats.action_dim[aid]
    data = {k: jnp.zeros((*basic_shape, *v), dtypes[k]) 
        for k, v in shapes.items()}
    data = dict2AttrDict(data)
    data.setdefault('global_state', data.obs)
    data.setdefault('hidden_state', data.obs)
    data.action = jnp.zeros((*basic_shape, action_dim), jnp.float32)
    data.hx = get_hx(data.sid, data.idx, data.event)

    return data

class Model(ModelBase):
    def add_attributes(self):
        self.imaginary_params = dict2AttrDict({'imaginary': True})
        self.params.imaginary = False

    def build_nets(self):
        aid = self.config.get('aid', 0)
        self.is_action_discrete = self.env_stats.is_action_discrete[aid]
        data = construct_fake_data(self.env_stats, aid)

        self.params.policy, self.modules.policy = self.build_net(
            data.obs, data.hx, data.action_mask, name='policy')
        self.params.value, self.modules.value = self.build_net(
            data.global_state, data.hx, name='value')
        self.sync_imaginary_params()

    @property
    def theta(self):
        return self.params

    def switch_params(self):
        self.params, self.imaginary_params = self.imaginary_params, self.params

    def sync_imaginary_params(self):
        for k, v in self.params.items():
            if k != 'imaginary':
                self.imaginary_params[k] = v

    def perturb_imaginary_params(self):
        self.imaginary_params.policy = self.params.policy.copy()
        chex.assert_trees_all_equal(self.params.policy, self.imaginary_params.policy)
        for k, v in self.imaginary_params.policy.items():
            init = nn.initializers.normal(self.config.rn_std, dtype=jnp.float32)
            self.act_rng, rng = random.split(self.act_rng)
            self.imaginary_params.policy[k] = v + init(rng, v.shape)

    def raw_action(
        self, 
        params, 
        rng, 
        data, 
        evaluation=False, 
    ):
        data.hx = self.build_hx(data)
        rngs = random.split(rng, 3)
        act_dist = self.policy_dist(params.policy, rngs[0], data, evaluation)

        if self.is_action_discrete:
            pi = nn.softmax(act_dist.logits)
            stats = {'mu': pi}
        else:
            mean = act_dist.mean()
            std = lax.exp(act_dist.logstd)
            stats = {
                'mu_mean': mean,
                'mu_std': jnp.broadcast_to(std, mean.shape), 
            }

        state = data.state
        if evaluation:
            action = act_dist.mode()
            stats.action = action
            return action, stats, state
        else:
            action = act_dist.sample(rng=rngs[1])
            logprob = act_dist.log_prob(action)
            value = self.modules.value(
                params.value, 
                rngs[2], 
                data.get('global_state', data.obs), 
                hx=data.hx
            )
            stats.update({'mu_logprob': logprob, 'value': value})

            return action, stats, state

    def policy_dist(self, params, rng, data, evaluation):
        act_out = self.modules.policy(
            params, 
            rng, 
            data.obs, 
            hx=data.hx, 
            action_mask=data.action_mask, 
        )
        if self.is_action_discrete:
            if evaluation and self.config.eval_act_temp > 0:
                act_out = act_out / self.config.eval_act_temp
            dist = jax_dist.Categorical(act_out)
        else:
            mu, logstd = act_out
            if evaluation and self.config.eval_act_temp > 0:
                logstd = logstd + math.log(self.config.eval_act_temp)
            dist = jax_dist.MultivariateNormalDiag(mu, logstd)

        return dist

    def build_hx(self, data):
        return get_hx(data.sid, data.idx, data.event)


def setup_config_from_envstats(config, env_stats):
    if 'aid' in config:
        aid = config['aid']
        config.policy.action_dim = env_stats.action_dim[aid]
        config.policy.is_action_discrete = env_stats.is_action_discrete[aid]
    else:
        config.policy.action_dim = env_stats.action_dim
        config.policy.is_action_discrete = env_stats.is_action_discrete
        config.policy.action_low = env_stats.action_low
        config.policy.action_high = env_stats.action_high

    return config


def create_model(
    config, 
    env_stats, 
    name='zero', 
    **kwargs
): 
    config = setup_config_from_envstats(config, env_stats)

    return Model(
        config=config, 
        env_stats=env_stats, 
        name=name,
        **kwargs
    )


if __name__ == '__main__':
    from tools.yaml_op import load_config
    from env.func import create_env
    from tools.display import pwc
    config = load_config('algo/zero_mr/configs/magw_a2c')
    
    env = create_env(config.env)
    model = create_model(config.model, env.stats())
    data = construct_fake_data(env.stats(), 0)
    print(model.action(model.params, data))
    pwc(hk.experimental.tabulate(model.raw_action)(model.params, data), color='yellow')
