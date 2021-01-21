import tensorflow as tf

from utility.rl_loss import n_step_target, quantile_regression_loss
from core.decorator import override
from algo.dqn.base import DQNBase, get_data_format, collect


class Agent(DQNBase):
    @override(DQNBase)
    @tf.function
    def _learn(self, obs, action, reward, next_obs, discount, steps=1, IS_ratio=1):
        terms = {}
        # compute the target
        next_x = self.target_encoder(next_obs)
        _, next_qt_embed = self.target_quantile(next_x, self.N_PRIME)
        if self._double:
            next_x_online = self.encoder(next_obs)
            _, next_qt_embed_online = self.quantile(next_x_online, self.N)
            next_action = self.q.action(next_x_online, next_qt_embed_online)
            next_qtv = self.target_q(next_x, next_qt_embed, action=next_action)
        else:
            next_action = self.target_q.action(next_x, next_qt_embed)
            next_qtvs = self.target_q(next_x, next_qt_embed)
            next_action = tf.argmax(next_qtvs, axis=-1, output_type=tf.int32)
            next_action = tf.one_hot(next_action, next_qtvs.shape[-1], dtype=next_qtvs.dtype)
            next_qtv = tf.reduce_sum(next_qtvs * next_action, axis=-1)
        reward = reward[:, None]
        discount = discount[:, None]
        if not isinstance(steps, int):
            steps = steps[:, None]
        target = n_step_target(reward, next_qtv, discount, self._gamma, steps)
        target = tf.expand_dims(target, axis=1)      # [B, 1, N']
        tf.debugging.assert_shapes([
            [next_qtv, (None, self.N_PRIME)],
            [target, (None, 1, self.N_PRIME)],
        ])

        with tf.GradientTape() as tape:
            x = self.encoder(obs)
            tau_hat, qt_embed = self.quantile(x, self.N)
            qtv = self.q(x, qt_embed, action)
            qtv = tf.expand_dims(qtv, axis=-1)  # [B, N, 1]
            error, qr_loss = quantile_regression_loss(
                qtv, target, tau_hat, kappa=self.KAPPA, return_error=True)
            loss = tf.reduce_mean(IS_ratio * qr_loss)

        terms['norm'] = self._optimizer(tape, loss)

        if self._is_per:
            error = tf.abs(error)
            error = tf.reduce_max(tf.reduce_mean(error, axis=-1), axis=-1)
            priority = self._compute_priority(error)
            terms['priority'] = priority
        
        terms.update(dict(
            target=target,
            q=tf.reduce_mean(qtv),
            qr_loss=qr_loss,
            loss=loss,
        ))

        return terms
