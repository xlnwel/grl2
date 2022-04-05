import copy
import functools
import logging
from typing import Dict
import numpy as np

from .trainloop import PPOTrainingLoop, PPGTrainingLoop
from core.elements.strategy import Strategy, create_strategy as raw_create_strategy
from core.mixin.strategy import Memory


logger = logging.getLogger(__name__)


class PPOStrategy(Strategy):
    def _post_init(self):
        self._memories = {}

    """ Calling Methods """
    def _prepare_input_to_actor(self, env_output):
        """ Extract data from env_output as the input 
        to Actor for inference """
        inp = self._add_memory_state_to_input(env_output)
        return inp

    def _record_output(self, out):
        states = out[-1]
        for i, memory in enumerate(self._last_memories):
            state = self.model.state_type(*[s[i:i+1] for s in states])
            memory.reset_states(state)

    """ PPO Methods """
    def record_inputs_to_vf(self, env_output):
        env_output = copy.deepcopy(env_output)
        self._value_input = self._add_memory_state_to_input(env_output)

    def compute_logits_values(self, value_inp: Dict[str, np.ndarray]=None):
        # be sure you normalize obs first if obs normalization is required
        action_type_logits, card_rank_logits, value = \
            self.model.compute_logits_values(**value_inp)
        return action_type_logits.numpy(), card_rank_logits.numpy(), value.numpy()

    def aux_train_record(self):
        stats = self.train_loop.aux_train_record()
        return stats

    def _add_memory_state_to_input(self, env_output):
        inp = env_output.obs.copy()
        eids = inp.pop('eid')
        pids = inp.pop('pid')
        masks = inp['mask']

        states = []
        self._last_memories = []
        for eid, pid, m in zip(eids, pids, masks):
            if eid not in self._memories:
                self._memories[eid] = [Memory(self.model) for _ in range(4)]
            self._last_memories.append(self._memories[eid][pid])
            state = self._memories[eid][pid].get_states_for_inputs(
                batch_size=1, sequential_dim=1
            )
            state = self._memories[eid][pid].apply_mask_to_state(state, m)
            states.append(state)
        state = self.model.state_type(*[np.concatenate(s) for s in zip(*states)])

        inp['state'] = state
        
        return inp


def create_strategy(name, config, actor=None, trainer=None, dataset=None):
    training = config['train_loop'].get('training', 'ppo')
    if training == 'pbt': 
        creator = functools.partial(
            raw_create_strategy, 
            strategy_cls=PPOStrategy,
            training_loop_cls=PPGTrainingLoop
        )
    elif training == 'ppo': 
        creator = functools.partial(
            raw_create_strategy, 
            strategy_cls=PPOStrategy,
            training_loop_cls=PPOTrainingLoop
        )
    elif training == 'ppg':
        creator = functools.partial(
            raw_create_strategy, 
            strategy_cls=PPOStrategy,
            training_loop_cls=PPGTrainingLoop
        )
    else:
        raise ValueError(training)
    
    return creator(name, config, actor, trainer, dataset)