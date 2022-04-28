from core.elements.trainer import TrainerEnsemble
from core.decorator import override
from core.tf_config import build
from .utils import get_data_format
from algo.hm.elements.trainer import MAPPOActorTrainer, MAPPOValueTrainer


class MAPPOTrainerEnsemble(TrainerEnsemble):
    @override(TrainerEnsemble)
    def _build_train(self, env_stats):
        # Explicitly instantiate tf.function to avoid unintended retracing
        TensorSpecs = get_data_format(self.config, env_stats, self.model)
        self.train = build(self.train, TensorSpecs)
        return True

    def raw_train(
        self, 
        obs, 
        global_state, 
        action, 
        reward, 
        value, 
        value_a, 
        traj_ret, 
        traj_ret_a, 
        raw_adv, 
        advantage, 
        target_prob, 
        tr_prob, 
        logprob, 
        prev_reward, 
        prev_action, 
        action_mask=None, 
        life_mask=None, 
        actor_state=None, 
        value_state=None, 
        mask=None
    ):
        actor_terms = self.policy.raw_train(
            obs=obs, 
            action=action, 
            advantage=advantage, 
            target_prob=target_prob, 
            tr_prob=tr_prob, 
            logprob=logprob, 
            prev_reward=prev_reward, 
            prev_action=prev_action, 
            state=actor_state, 
            action_mask=action_mask, 
            life_mask=life_mask, 
            mask=mask
        )

        value_terms = self.value.raw_train(
            global_state=global_state, 
            action=action, 
            pi=actor_terms['pi'], 
            value=value, 
            value_a=value_a, 
            traj_ret=traj_ret, 
            traj_ret_a=traj_ret_a, 
            prev_reward=prev_reward, 
            prev_action=prev_action, 
            state=value_state, 
            life_mask=life_mask,
            mask=mask
        )

        return {**actor_terms, **value_terms}


def create_trainer(config, env_stats, loss, name='mappo'):
    def constructor(config, env_stats, cls, name):
        return cls(
            config=config, 
            env_stats=env_stats, 
            loss=loss[name], 
            name=name)

    return MAPPOTrainerEnsemble(
        config=config,
        env_stats=env_stats,
        loss=loss,
        constructor=constructor,
        name=name,
        policy=MAPPOActorTrainer,
        value=MAPPOValueTrainer,
    )
