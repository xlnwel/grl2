import numpy as np
import tensorflow as tf

from utility.display import pwc
from utility.schedule import TFPiecewiseSchedule
from core.tf_config import build
from core.base import BaseAgent
from core.decorator import agent_config
from core.optimizers import Optimizer
from algo.ppo.loss import compute_ppo_loss, compute_value_loss


class Agent(BaseAgent):
    @agent_config
    def __init__(self, env):
        # optimizer
        if getattr(self, 'schedule_lr', False):
            self._learning_rate = TFPiecewiseSchedule([(300, self._learning_rate), (1000, 5e-5)])

        self._optimizer = Optimizer(
            self._optimizer, self.ac, self._learning_rate, 
            epsilon=self._epsilon)
        self._ckpt_models['optimizer'] = self._optimizer


        # Explicitly instantiate tf.function to avoid unintended retracing
        TensorSpecs = [
            (env.obs_shape, env.obs_dtype, 'obs'),
            (env.action_shape, env.action_dtype, 'action'),
            ((), tf.float32, 'traj_ret'),
            ((), tf.float32, 'value'),
            ((), tf.float32, 'advantage'),
            ((), tf.float32, 'old_logpi'),
            ((), tf.float32, 'mask'),
            ((), tf.float32, 'n'),
        ]
        self.learn = build(
            self._learn, 
            TensorSpecs, 
            sequential=True, 
            batch_size=env.n_envs,
        )

    def reset_states(self, states=None):
        self.ac.reset_states(states)

    def step(self, obs, deterministic=False):
        obs = tf.convert_to_tensor(obs, tf.float32)
        if deterministic:
            action = self.ac.det_action(obs)
            return action
        else:
            action, logpi, value = self.ac.step(obs)
            return action, logpi, value
    def learn_log(self, buffer, epoch):
        for i in range(self._n_updates):
            self.ac.reset_states()
            for j in range(buffer.n_minibatches):
                data = buffer.sample()
                data['n'] = n = np.sum(data['mask'])
                value = data['value']
                data = {k: tf.convert_to_tensor(v, tf.float32) for k, v in data.items()}
                with tf.name_scope('train'):
                    terms = self.learn(**data)
    
                n_total_trans = value.size
                n_valid_trans = n or n_total_trans

                terms['value'] = np.mean(value)
                terms['n_valid_trans'] = n_valid_trans
                terms['n_total_trans'] = n_total_trans
                terms['valid_trans_frac'] = n_valid_trans / n_total_trans

                approx_kl = terms['approx_kl']
                del terms['approx_kl']

                self.store(**terms)
            
            if getattr(self, '_max_kl', 0) > 0 and approx_kl > self._max_kl:
                pwc(f'Eearly stopping at epoch-{epoch} update-{i+1} due to reaching max kl.',
                    f'Current kl={approx_kl:.3g}', color='blue')
                break
        self.store(approx_kl=approx_kl)
        if not isinstance(self._learning_rate, float):
            self.store(learning_rate=self._learning_rate(tf.cast(self.global_steps, tf.float32)))

    @tf.function
    def _learn(self, obs, action, traj_ret, value, advantage, old_logpi, mask=None, n=None):
        old_value = value
        with tf.GradientTape() as tape:
            action_dist, value = self.ac.train_step(obs)
            logpi = action_dist.log_prob(action)
            entropy = action_dist.entropy()
            # policy loss
            ppo_loss, entropy, approx_kl, p_clip_frac = compute_ppo_loss(
                logpi, old_logpi, advantage, self._clip_range,
                entropy, mask=mask, n=n)
            # value loss
            value_loss, v_clip_frac = compute_value_loss(
                value, traj_ret, old_value, self._clip_range,
                mask=mask, n=n)

            with tf.name_scope('total_loss'):
                policy_loss = (ppo_loss 
                        - self._entropy_coef * entropy # TODO: adaptive entropy regularizer
                        + self._kl_coef * approx_kl)
                value_loss = self._value_coef * value_loss
                total_loss = policy_loss + value_loss

        terms = dict(
            entropy=entropy, 
            approx_kl=approx_kl, 
            p_clip_frac=p_clip_frac,
            v_clip_frac=v_clip_frac,
            ppo_loss=ppo_loss,
            value_loss=value_loss
        )
        terms['grads_norm'] = self._optimizer(tape, total_loss)

        return terms