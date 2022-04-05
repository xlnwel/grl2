import os
from typing import Tuple
import tensorflow as tf

from core.elements.model import Model
from core.tf_config import build
from utility.file import source_file

# register ppo-related networks 
source_file(os.path.realpath(__file__).replace('model.py', 'nn.py'))


class PPOModel(Model):
    def _pre_init(self):
        if self.config.encoder.nn_id.startswith('cnn'):
            self.config.encoder.time_distributed = 'rnn' in self.config

    def _build(self, env_stats, evaluation=False):
        basic_shape = (None,)
        dtype = tf.keras.mixed_precision.experimental.global_policy().compute_dtype
        shapes = env_stats['obs_shape']
        dtypes = env_stats['obs_dtype']
        TensorSpecs = {k: ((*basic_shape, *v), dtypes[k], k) 
            for k, v in shapes.items()}

        if self.state_size:
            TensorSpecs.update(dict(
                state=self.state_type(*[((None, sz), dtype, name) 
                    for name, sz in self.state_size._asdict().items()]),
                mask=(basic_shape, tf.float32, 'mask'),
                evaluation=evaluation,
                return_eval_stats=evaluation,
            ))
        self.action = build(self.action, TensorSpecs)

    @tf.function
    def action(
        self, 
        obs, 
        state: Tuple[tf.Tensor]=None,
        mask: tf.Tensor=None,
        evaluation=False, 
        return_eval_stats=False
    ):
        x, state = self.encode(obs, state=state, mask=mask)
        act_dist = self.policy(x, evaluation=evaluation)
        action = self.policy.action(act_dist, evaluation)

        if evaluation:
            value = self.value(x)
            return action, {'value': value}, state
        else:
            logpi = act_dist.log_prob(action)
            value = self.value(x)
            terms = {'logpi': logpi, 'value': value}

            return action, terms, state    # keep the batch dimension for later use

    @tf.function
    def compute_value(
        self, 
        obs, 
        state: Tuple[tf.Tensor]=None,
        mask: tf.Tensor=None
    ):
        x, state = self.encode(obs, state, mask)
        value = self.value(x)
        return value, state

    def encode(self, 
                obs, 
                state: Tuple[tf.Tensor]=None,
                mask: tf.Tensor=None):
        x = self.encoder(obs)
        if hasattr(self, 'rnn'):
            x, state = self.rnn(x, state, mask)
            return x, state
        else:
            return x, None


def create_model(
        config, 
        env_stats, 
        name='ppo', 
        to_build=False,
        to_build_for_eval=False,
        **kwargs):
    config.policy.action_dim = env_stats.action_dim
    config.policy.is_action_discrete = env_stats.action_dim

    return PPOModel(
        config=config, 
        env_stats=env_stats, 
        name=name,
        to_build=to_build, 
        to_build_for_eval=to_build_for_eval,
        **kwargs
    )