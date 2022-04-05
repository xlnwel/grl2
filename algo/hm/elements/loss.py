import tensorflow as tf

from core.elements.loss import Loss, LossEnsemble
from utility.rl_loss import huber_loss, reduce_mean, ppo_loss, clipped_value_loss
from utility.tf_utils import explained_variance


class PPOLossImpl(Loss):
    def _compute_value_loss(
        self, 
        value, 
        traj_ret, 
        old_value, 
        mask=None
    ):
        value_loss_type = getattr(self.config, 'value_loss', 'mse')
        v_clip_frac = 0
        if value_loss_type == 'huber':
            value_loss = reduce_mean(
                huber_loss(value, traj_ret, threshold=self.config.huber_threshold), mask)
        elif value_loss_type == 'mse':
            value_loss = .5 * reduce_mean((value - traj_ret)**2, mask)
        elif value_loss_type == 'clip':
            value_loss, v_clip_frac = clipped_value_loss(
                value, traj_ret, old_value, self.config.clip_range, 
                mask=mask)
        elif value_loss_type == 'clip_huber':
            value_loss, v_clip_frac = clipped_value_loss(
                value, traj_ret, old_value, self.config.clip_range, 
                mask=mask, threshold=self.config.huber_threshold)
        else:
            raise ValueError(f'Unknown value loss type: {value_loss_type}')

        return value_loss, v_clip_frac


class MAPPOPolicyLoss(Loss):
    def loss(
        self, 
        obs, 
        action, 
        advantage, 
        logpi, 
        prev_reward=None, 
        prev_action=None, 
        state=None, 
        action_mask=None, 
        life_mask=None, 
        mask=None
    ):
        with tf.GradientTape() as tape:
            x, _ = self.model.encode(
                x=obs, 
                prev_reward=prev_reward, 
                prev_action=prev_action, 
                state=state, 
                mask=mask
            )
            act_dist = self.policy(x, action_mask)
            new_logpi = act_dist.log_prob(action)
            entropy = act_dist.entropy()
            log_ratio = new_logpi - logpi
            policy_loss, entropy, kl, clip_frac = ppo_loss(
                log_ratio, advantage, self.config.clip_range, entropy, 
                life_mask if self.config.life_mask else None)
            entropy_loss = - self.config.entropy_coef * entropy
            loss = policy_loss + entropy_loss

        terms = dict(
            prev_reward=prev_reward,
            ratio=tf.exp(log_ratio),
            entropy=entropy,
            kl=kl,
            logpi=logpi,
            new_logpi=new_logpi, 
            p_clip_frac=clip_frac,
            policy_loss=policy_loss,
            entropy_loss=entropy_loss, 
            actor_loss=loss,
            adv_std=tf.math.reduce_std(advantage, axis=-1), 
        )
        if action_mask is not None:
            terms['n_avail_actions'] = tf.reduce_sum(tf.cast(action_mask, tf.float32), -1)
        if life_mask is not None:
            terms['life_mask'] = life_mask
        if not self.policy.is_action_discrete:
            terms['std'] = tf.exp(self.policy.logstd)

        return tape, loss, terms


class MAPPOValueLoss(PPOLossImpl):
    def loss(
        self, 
        global_state, 
        value, 
        traj_ret, 
        prev_reward=None, 
        prev_action=None, 
        state=None, 
        life_mask=None, 
        mask=None
    ):
        old_value = value
        with tf.GradientTape() as tape:
            value, _ = self.model.compute_value(
                global_state=global_state,
                prev_reward=prev_reward,
                prev_action=prev_action,
                state=state,
                mask=mask
            )

            loss, clip_frac = self._compute_value_loss(
                value=value, 
                traj_ret=traj_ret, 
                old_value=old_value, 
                mask=life_mask if self.config.life_mask else None
            )
            loss = self.config.value_coef * loss

        terms = dict(
            value=value,
            v_loss=loss,
            explained_variance=explained_variance(traj_ret, value),
            traj_ret_std=tf.math.reduce_std(traj_ret, axis=-1), 
            v_clip_frac=clip_frac,
        )

        return tape, loss, terms


def create_loss(config, model, name='mappo'):
    def constructor(config, cls, name):
        return cls(
            config=config, 
            model=model[name], 
            name=name)

    return LossEnsemble(
        config=config,
        model=model,
        constructor=constructor,
        name=name,
        policy=MAPPOPolicyLoss,
        value=MAPPOValueLoss,
    )