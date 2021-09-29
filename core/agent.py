from env.typing import EnvOutput
from typing import Union
import logging

from core.checkpoint import *
from core.decorator import config, record, step_track
from core.mixin.agent import StepCounter, TensorboardOps
from core.module import Model, ModelEnsemble, Trainer, TrainerEnsemble, Actor
from core.log import *
from utility.timer import Every, Timer

logger = logging.getLogger(__name__)


def set_attr(obj, name, attr):
    setattr(obj, name, attr)
    if isinstance(attr, dict):
        for k, v in attr.items():
            if not k.endswith(name):
                raise ValueError(f'Inconsistent error: {k} does not ends with {name}')
            setattr(obj, k, v)


class AgentBase(StepCounter, TensorboardOps):
    """ Initialization """
    @config
    @record
    def __init__(self, 
                 *, 
                 env_stats, 
                 model: Union[Model, ModelEnsemble], 
                 trainer: Union[Trainer, TrainerEnsemble], 
                 actor: Actor,
                 dataset=None):
        set_attr(self, 'model', model)
        set_attr(self, 'trainer', trainer)
        self.actor = actor
        self.dataset = dataset

        self._post_init(env_stats, dataset)
        self.restore()

    def _post_init(self, env_stats, dataset):
        """ Adds attributes to Agent """
        self._sample_timer = Timer('sample')
        self._learn_timer = Timer('train')

        self._return_stats = getattr(self, '_return_stats', False)

        self.RECORD = getattr(self, 'RECORD', False)
        self.N_EVAL_EPISODES = getattr(self, 'N_EVAL_EPISODES', 1)

        # intervals between calling self._summary
        self._to_summary = Every(self.LOG_PERIOD, self.LOG_PERIOD)
        
        self._initialize_counter()

    def reset_states(self, states=None):
        pass

    def get_states(self):
        pass

    def _summary(self, data, terms):
        """ Adds non-scalar summaries here """
        pass

    """ Call """
    def __call__(self, 
                 env_output: EnvOutput, 
                 evaluation: bool=False,
                 return_eval_stats: bool=False):
        inp = self._prepare_input_to_actor(env_output)
        out = self.actor(inp, evaluation=evaluation, 
            return_eval_stats=return_eval_stats)
        self._record_output(out)
        return out[:2]

    def _prepare_input_to_actor(self, env_output):
        """ Extract data from env_output as the input 
        to Actor for inference """
        inp = env_output.obs
        return inp

    def _record_output(self, out):
        """ Record some data in out """
        pass

    """ Train """
    @step_track
    def train_log(self, step):
        n = self._sample_train()
        self._store_additional_stats()

        return n

    def _sample_train(self):
        raise NotImplementedError
    
    def _store_additional_stats(self):
        pass

    """ Checkpoint Ops """
    def restore(self):
        """ Restore model """
        if getattr(self, 'trainer', None) is not None:
            self.trainer.restore()
        elif getattr(self, 'model', None) is not None:
            self.model.restore()
        self.actor.restore_auxiliary_stats()
        self.restore_step()

    def save(self, print_terminal_info=False):
        """ Save model """
        if getattr(self, 'trainer', None) is not None:
            self.trainer.save(print_terminal_info)
        elif getattr(self, 'model', None) is not None:
            self.model.save(print_terminal_info)
        self.actor.save_auxiliary_stats()
        self.save_step()
