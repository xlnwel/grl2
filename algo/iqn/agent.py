import numpy as np
import tensorflow as tf

from utility.display import pwc
from utility.rl_utils import n_step_target, huber_loss
from utility.utils import Every
from utility.schedule import TFPiecewiseSchedule, PiecewiseSchedule
from utility.timer import TBTimer
from core.tf_config import build
from core.base import BaseAgent
from core.decorator import agent_config, step_track
from core.optimizer import Optimizer
from algo.dqn.agent import get_data_format


class Agent(BaseAgent):
    @agent_config
    def __init__(self, *, dataset, env):
        self._is_per = self._replay_type.endswith('per')
        is_nsteps = dataset and 'steps' in dataset.data_format
        self.dataset = dataset

        if self._schedule_lr:
            self._lr = TFPiecewiseSchedule(
                [(5e5, self._lr), (2e6, 5e-5)], outside_value=5e-5)
        if self._schedule_act_eps:
            self._act_eps = PiecewiseSchedule(((5e4, 1), (4e6, .01)))

        self._to_sync = Every(self._target_update_period)
        # optimizer
        self._optimizer = Optimizer(self._optimizer, self.q, self._lr, clip_norm=self._clip_norm)

        self._action_dim = env.action_dim

        # Explicitly instantiate tf.function to initialize variables
        obs_dtype = env.obs_dtype if len(env.obs_shape) == 3 else self._dtype
        TensorSpecs = dict(
            obs=(env.obs_shape, env.obs_dtype, 'obs'),
            action=((self._action_dim,), tf.float32, 'action'),
            reward=((), self._dtype, 'reward'),
            next_obs=(env.obs_shape, env.obs_dtype, 'next_obs'),
            discount=((), self._dtype, 'discount'),
        )
        if self._is_per:
            TensorSpecs['IS_ratio'] = ((), self._dtype, 'IS_ratio')
        if is_nsteps:
            TensorSpecs['steps'] = ((), self._dtype, 'steps')
        self.learn = build(self._learn, TensorSpecs, batch_size=self._batch_size)

        self._sync_target_nets()

        self._to_summary = Every(self.LOG_PERIOD, self.LOG_PERIOD)

    def reset_noisy(self):
        pass

    def __call__(self, x, deterministic=False, **kwargs):
        if self._schedule_act_eps:
            eps = self._act_eps.value(self.env_step)
            self.store(act_eps=eps)
        else:
            eps = self._act_eps
        eps = tf.convert_to_tensor(eps, tf.float32)
        
        x = np.array(x)
        if len(x.shape) % 2 != 0:
            x = tf.expand_dims(x, 0)

        action = self.action(x, deterministic, eps)
        action = np.squeeze(action.numpy())

        return action

    @tf.function
    def action(self, x, deterministic=False, epsilon=0):
        action = self.q.action(x, self.K)
        if not deterministic and epsilon > 0:
            rand_act = tf.random.uniform(
                action.shape, 0, self._action_dim, dtype=tf.int32)
            action = tf.where(
                tf.random.uniform(action.shape, 0, 1) < epsilon,
                rand_act, action)
        
        return action

    @step_track
    def learn_log(self, step):
        for _ in range(self.N_UPDATES):
            data = self.dataset.sample()

            if self._is_per:
                idxes = data.pop('idxes').numpy()

            terms = self.learn(**data)
            if self._to_sync(self.train_step):
                self._sync_target_nets()

            if self._schedule_lr:
                step = tf.convert_to_tensor(step, tf.float32)
                terms['lr'] = self._lr(step)
            terms = {k: v.numpy() for k, v in terms.items()}

            if self._is_per:
                self.dataset.update_priorities(terms['priority'], idxes)
            self.store(**terms)
        if self._to_summary(self.train_step):
            self.summary(data)

        return self.N_UPDATES

    @tf.function
    def summary(self, data):
        self.histogram_summary({'steps': data['steps']}, step=self._env_step)
        if 'IS_ratio' in data:
            self.histogram_summary({'IS_ratio': data['IS_ratio']}, step=self._env_step)

    @tf.function
    def _learn(self, obs, action, reward, next_obs, discount, steps=1, IS_ratio=1):
        terms = {}
        with tf.GradientTape() as tape:
            tau_hat, qtv, q = self.q.value(obs, self.N, action)
            next_action = self.q.action(next_obs, self.K)
            _, next_qtv, _ = self.target_q.value(next_obs, self.N_PRIME, next_action)
            reward = reward[:, None, None]
            discount = discount[:, None, None]
            if not isinstance(steps, int):
                steps = steps[:, None, None]
            returns = n_step_target(reward, next_qtv, discount, self._gamma, steps, self._tbo)
            tf.debugging.assert_shapes([
                [qtv, (None, self.N, 1)],
                [returns, (None, self.N_PRIME, 1)]
            ])
            returns = tf.stop_gradient(returns)

            error = returns - qtv   # [B, N, N']
            
            # loss
            tau_hat = tf.transpose(tf.reshape(tau_hat, [self.N, self._batch_size, 1]), [1, 0, 2]) # [B, N, 1]
            weight = tf.abs(tau_hat - tf.cast(error < 0, tf.float32))        # [B, N, N']
            huber = huber_loss(error, threshold=self.KAPPA)             # [B, N, N']
            qr_loss = tf.reduce_sum(tf.reduce_mean(weight * huber, axis=2), axis=1) # [B]
            loss = tf.reduce_mean(qr_loss)

        if self._is_per:
            error = tf.reduce_max(tf.reduce_mean(tf.abs(error), axis=2), axis=1)
            priority = self._compute_priority(error)
            terms['priority'] = priority
        
        terms['norm'] = self._optimizer(tape, loss)
        
        terms.update(dict(
            q=q,
            returns=returns,
            loss=loss,
        ))

        return terms

    def _compute_priority(self, priority):
        """ p = (p + 𝝐)**𝛼 """
        priority += self._per_epsilon
        priority **= self._per_alpha
        return priority

    @tf.function
    def _sync_target_nets(self):
        [tv.assign(mv) for mv, tv in zip(
            self.q.variables, self.target_q.variables)]
