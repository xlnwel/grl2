import functools
import numpy as np
import tensorflow as tf

from utility.rl_loss import n_step_target, quantile_regression_loss
from utility.tf_utils import explained_variance
from utility.schedule import TFPiecewiseSchedule
from core.optimizer import Optimizer
from core.decorator import override
from algo.dqn.base import DQNBase, get_data_format, collect


class Agent(DQNBase):
    """ Initialization """
    @override(DQNBase)
    def _construct_optimizers(self):
        if self._schedule_lr:
            assert isinstance(self._actor_lr, list), self._actor_lr
            assert isinstance(self._value_lr, list), self._value_lr
            self._actor_lr = TFPiecewiseSchedule(self._actor_lr)
            self._value_lr = TFPiecewiseSchedule(self._value_lr)

        PartialOpt = functools.partial(
            Optimizer,
            name=self._optimizer,
            weight_decay=self._weight_decay, 
            clip_norm=self._clip_norm,
            epsilon=self._epsilon
        )
        self._actor_opt = PartialOpt(models=self.actor, lr=self._actor_lr)
        value_models = [self.encoder, self.q]
        self._value_opt = PartialOpt(models=value_models, lr=self._value_lr)

        if self.temperature.is_trainable():
            self._temp_opt = Optimizer(self._optimizer, self.temperature, self._temp_lr)
            if isinstance(self._target_entropy_coef, (list, tuple)):
                self._target_entropy_coef = TFPiecewiseSchedule(self._target_entropy_coef)

    # @tf.function
    # def summary(self, data, terms):
    #     tf.summary.histogram('learn/entropy', terms['entropy'], step=self._env_step)
    #     tf.summary.histogram('learn/reward', data['reward'], step=self._env_step)
    """ Call """
    def _process_input(self, obs, evaluation, env_output):
        obs, kwargs = super()._process_input(obs, evaluation, env_output)
        kwargs['temp'] = self._eval_act_temp if evaluation else self._act_temp
        return obs, kwargs

    """ SACIQN Methods"""
    @override(DQNBase)
    @tf.function
    def _learn(self, obs, action, reward, next_obs, discount, steps=1, IS_ratio=1):
        terms = {}
        if self.temperature.type == 'schedule':
            _, temp = self.temperature(self._train_step)
        elif self.temperature.type == 'state-action':
            raise NotImplementedError
        else:
            _, temp = self.temperature()

        # compute q_target
        next_x = self.target_encoder(next_obs, training=False)
        next_act_probs, next_act_logps = self.target_actor.train_step(next_x)
        next_act_probs_ext = tf.expand_dims(next_act_probs, axis=1)  # [B, 1, A]
        next_act_logps_ext = tf.expand_dims(next_act_logps, axis=1)  # [B, 1, A]
        _, qt_embed = self.target_quantile(next_x, self.N_PRIME)
        next_x_ext = tf.expand_dims(next_x, axis=1)
        next_qtv = self.target_q(next_x_ext, qt_embed)
        
        if self._soft_target:
            next_qtv_v = tf.reduce_sum(next_act_probs_ext 
                * (next_qtv - temp * next_act_logps_ext), axis=-1)
        else:
            next_qtv_v = tf.reduce_sum(next_act_probs_ext * next_qtv, axis=-1)
        reward = reward[:, None]
        discount = discount[:, None]
        if not isinstance(steps, int):
            steps = steps[:, None]
        q_target = n_step_target(reward, next_qtv_v, discount, self._gamma, steps)
        q_target = tf.expand_dims(q_target, axis=1)      # [B, 1, N']
        tf.debugging.assert_shapes([
            [next_qtv_v, (None, self.N_PRIME)],
            [next_act_probs_ext, (None, 1, self._action_dim)],
            [next_act_logps_ext, (None, 1, self._action_dim)],
            [q_target, (None, 1, self.N_PRIME)],
        ])

        with tf.GradientTape() as tape:
            x = self.encoder(obs, training=True)
            tau_hat, qt_embed = self.quantile(x, self.N)
            x_ext = tf.expand_dims(x, axis=1)
            action_ext = tf.expand_dims(action, axis=1)
            # q loss
            qtvs, qs = self.q(x_ext, qt_embed, return_value=True)
            qtv = tf.reduce_sum(qtvs * action_ext, axis=-1, keepdims=True)  # [B, N, 1]
            error, qr_loss = quantile_regression_loss(
                qtv, q_target, tau_hat, kappa=self.KAPPA, return_error=True)
            qr_loss = tf.reduce_mean(IS_ratio * qr_loss)

        terms['value_norm'] = self._value_opt(tape, qr_loss)

        with tf.GradientTape() as tape:
            act_probs, act_logps = self.actor.train_step(x)
            q = tf.reduce_sum(act_probs * qs, axis=-1)
            entropy = - tf.reduce_sum(act_probs * act_logps, axis=-1)
            actor_loss = -(q + temp * entropy)
            actor_loss = tf.reduce_mean(IS_ratio * actor_loss)
        terms['actor_norm'] = self._actor_opt(tape, actor_loss)

        act_probs = tf.reduce_mean(act_probs, 0)
        # self.actor.update_prior(act_probs, self._prior_lr)
        if self.temperature.is_trainable():
            # Entropy of a uniform distribution
            self._target_entropy = np.log(self._action_dim)
            target_entropy_coef = self._target_entropy_coef \
                if isinstance(self._target_entropy_coef, float) \
                else self._target_entropy_coef(self._train_step)
            target_entropy = self._target_entropy * target_entropy_coef
            with tf.GradientTape() as tape:
                log_temp, temp = self.temperature(x, action)
                entropy_diff = target_entropy - entropy
                temp_loss = -log_temp * entropy_diff
                tf.debugging.assert_shapes([[temp_loss, (None, )]])
                temp_loss = tf.reduce_mean(IS_ratio * temp_loss)
            terms['target_entropy'] = target_entropy
            terms['entropy_diff'] = entropy_diff
            terms['log_temp'] = log_temp
            terms['temp_loss'] = temp_loss
            terms['temp_norm'] = self._temp_opt(tape, temp_loss)

        if self._is_per:
            error = tf.abs(error)
            error = tf.reduce_max(tf.reduce_mean(error, axis=-1), axis=-1)
            priority = self._compute_priority(error)
            terms['priority'] = priority
            
        q_target = tf.reduce_mean(q_target, axis=(1, 2))
        terms.update(dict(
            steps=steps,
            reward_min=tf.reduce_min(reward),
            actor_loss=actor_loss,
            q=q,
            entropy=entropy,
            entropy_max=tf.reduce_max(entropy),
            entropy_min=tf.reduce_min(entropy),
            qr_loss=qr_loss, 
            temp=temp,
            explained_variance_q=explained_variance(q_target, q),
        ))
        # for i in range(self.actor.action_dim):
        #     terms[f'prior_{i}'] = self.actor.prior[i]

        return terms

    def _compute_qr_loss(self, q, x, embed, tau_hat, returns, action, IS_ratio):
        qtvs, value = q(x, embed, return_value=True)
        if action:
            assert qtvs.shape[-1] == self._action_dim, qtvs.shape
            qtv = tf.reduce_sum(qtvs * action, axis=-1, keepdims=True)  # [B, N, 1]
        else:
            qtv = qtvs
        error, qr_loss = quantile_regression_loss(
            qtv, returns, tau_hat, kappa=self.KAPPA, return_error=True)
            
        tf.debugging.assert_shapes([
            [qtv, (None, self.N, 1)],
            [qr_loss, (None)],
        ])

        qr_loss = tf.reduce_mean(IS_ratio * qr_loss)

        return value, tf.abs(error), qr_loss
