import tensorflow as tf

from utility.tf_utils import softmax, log_softmax, explained_variance
from utility.rl_utils import *
from utility.rl_loss import retrace
from algo.mrdqn.base import RDQNBase, get_data_format, collect


class Agent(RDQNBase):
    """ MRDQN methods """
    @tf.function
    def _learn(self, obs, action, reward, discount, mu, mask, 
                IS_ratio=1, state=None, prev_action=None, prev_reward=None):
        obs, action, mu, mask, target, state, add_inp, terms = \
            self._compute_target_and_process_data(
                obs, action, reward, discount, mu, mask, 
                state, prev_action, prev_reward)

        with tf.GradientTape() as tape:
            x, _ = self._compute_embed(obs, mask, state, add_inp)
            
            qs = self.q(x)
            q = tf.reduce_sum(qs * action, -1)
            error = target - q
            value_loss = tf.reduce_mean(.5 * error**2, axis=-1)
            value_loss = tf.reduce_mean(IS_ratio * value_loss)
            terms['value_loss'] = value_loss
        tf.debugging.assert_shapes([
            [q, (None, self._sample_size)],
            [target, (None, self._sample_size)],
            [error, (None, self._sample_size)],
            [IS_ratio, (None,)],
            [value_loss, ()]
        ])
        terms['value_norm'] = self._value_opt(tape, value_loss)

        if 'actor' in self.model:
            with tf.GradientTape() as tape:
                pi, logpi = self.actor.train_step(x)
                pi_a = tf.reduce_sum(pi * action, -1)
                loo_loss = tf.minimum(1. / mu, self._loo_c) * error * pi_a \
                    + tf.reduce_sum(qs * pi, axis=-1)
                loo_loss = tf.reduce_mean(loo_loss, axis=-1)
                actor_loss = tf.reduce_mean(IS_ratio * loo_loss)
                terms['entropy'] = -tf.reduce_mean(pi * logpi)
                terms['actor_loss'] = actor_loss
                terms['ratio'] = tf.reduce_mean(pi_a / mu)
            tf.debugging.assert_shapes([
                [loo_loss, (None)],
            ])
            terms['actor_norm'] = self._actor_opt(tape, actor_loss)

        if self._is_per:
            priority = self._compute_priority(tf.abs(error))
            terms['priority'] = priority
        
        terms.update(dict(
            q=q,
            mu_min=tf.reduce_min(mu),
            mu=mu,
            mu_std=tf.math.reduce_std(mu),
            target=target,
            q_explained_variance=explained_variance(target, q)
        ))

        return terms
    
    def _compute_target(self, obs, action, reward, discount, 
                        mu, mask, state, add_inp):
        terms = {}
        x, _ = self._compute_embed(obs, mask, state, add_inp, online=False)
        if self._burn_in:
            bis = self._burn_in_size
            ss = self._sample_size - bis
            _, reward = tf.split(reward, [bis, ss], 1)
            _, discount = tf.split(discount, [bis, ss], 1)
            _, next_mu_a = tf.split(mu, [bis+1, ss], 1)
            _, next_x = tf.split(x, [bis+1, ss], 1)
            _, next_action = tf.split(action, [bis+1, ss], 1)
        else:
            _, next_mu_a = tf.split(mu, [1, self._sample_size], 1)
            _, next_x = tf.split(x, [1, self._sample_size], 1)
            _, next_action = tf.split(action, [1, self._sample_size], 1)

        next_qs = self.target_q(next_x)
        regularization = None
        if hasattr(self, 'actor'):
            next_pi, next_logpi = self.actor.train_step(next_x)
            if self._probabilistic_regularization == 'entropy':
                regularization = self._tau * tf.reduce_sum(next_pi * next_logpi, axis=-1)
            else:
                regularization = None
        else:
            if self._probabilistic_regularization is None:
                if self._double:    # don't suggest to use double Q here, but implement it anyway
                    online_x, _ = self._compute_embed(obs, mask, state, add_inp)
                    next_online_x = tf.split(online_x, [bis+1, ss-1], 1)
                    next_online_qs = self.q(next_online_x)
                    next_pi = self.compute_greedy_action(next_online_qs, one_hot=True)
                else:    
                    next_pi = self.target_q.compute_greedy_action(next_qs, one_hot=True)
            elif self._probabilistic_regularization == 'prob':
                next_pi = softmax(next_qs, self._tau)
            elif self._probabilistic_regularization == 'entropy':
                next_pi = softmax(next_qs, self._tau)
                next_logpi = log_softmax(next_qs, self._tau)
                regularization = tf.reduce_sum(next_pi * next_logpi, axis=-1)
                terms['next_entropy'] = - regularization / self._tau
            else:
                raise ValueError(self._probabilistic_regularization)

        discount = discount * self._gamma
        target = retrace(
            reward, next_qs, next_action, 
            next_pi, next_mu_a, discount,
            lambda_=self._lambda, 
            axis=1, tbo=self._tbo,
            regularization=regularization)

        return target, terms
