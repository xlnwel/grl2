from core.elements.trainer import Trainer, TrainerEnsemble
from core.decorator import override
from core.tf_config import build
from .utils import get_data_format


class MAPPOActorTrainer(Trainer):
    def raw_train(
        self, 
        obs, 
        action, 
        advantage, 
        logpi, 
        prev_reward, 
        prev_action, 
        state=None, 
        action_mask=None, 
        life_mask=None, 
        mask=None
    ):
        tape, loss, terms = self.loss.loss(
            obs, 
            action, 
            advantage, 
            logpi, 
            prev_reward, 
            prev_action, 
            state, 
            action_mask, 
            life_mask, 
            mask
        )
        
        terms['actor_norm'] = self.optimizer(tape, loss)

        return terms


class MAPPOValueTrainer(Trainer):
    def raw_train(
        self, 
        global_state, 
        value, 
        traj_ret, 
        prev_reward, 
        prev_action, 
        state=None, 
        life_mask=None, 
        mask=None
    ):
        tape, loss, terms = self.loss.loss(
            global_state, 
            value, 
            traj_ret, 
            prev_reward, 
            prev_action, 
            state, 
            life_mask, 
            mask
        )

        terms['value_norm'] = self.optimizer(tape, loss)

        return terms


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
        value, 
        traj_ret, 
        advantage, 
        logpi, 
        prev_reward, 
        prev_action, 
        action_mask=None, 
        life_mask=None, 
        state=None, 
        mask=None
    ):
        if state is None:
            actor_state, value_state = None, None
        else:
            actor_state, value_state = self.model.split_state(state)
        actor_terms = self.policy.raw_train(
            obs, action, advantage, logpi, prev_reward, prev_action, 
            actor_state, action_mask, life_mask, mask
        )
        value_terms = self.value.raw_train(
            global_state, value, traj_ret, prev_reward, prev_action, 
            value_state, life_mask, mask
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