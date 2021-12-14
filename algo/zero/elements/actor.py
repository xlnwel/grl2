from core.elements.actor import Actor
from core.mixin.actor import RMS
from core.typing import ModelPath


class PPOActor(Actor):
    def _post_init(self):
        self.config.rms.root_dir = self.config.root_dir
        self.config.rms.model_name = self.config.model_name
        self.config.algorithm = 'zero'
        self.rms = RMS(self.config.rms)

    def reset_model_path(self, model_path: ModelPath):
        self.config.rms.root_dir = model_path.root_dir
        self.config.rms.model_name = model_path.model_name
        self.rms = RMS(self.config.rms)

    """ Calling Methods """
    def _process_input(self, inp: dict, evaluation: bool):
        if self.config.algorithm == 'zero':
            inp.pop('others_h', None)
        inp = self.rms.process_obs_with_rms(inp, update_rms=False)
        inp, tf_inp = super()._process_input(inp, evaluation)
        state = tf_inp.pop('state')
        tf_inp.update({
            **state._asdict()
        })
        return inp, tf_inp

    def _process_output(self, inp, out, evaluation):
        action, terms, state = super()._process_output(inp, out, evaluation)
        if not evaluation:
            action = (*action, state[2])
            terms.update({
                **{k: inp[k] for k in self.rms.obs_names},
            })
        return action, terms, state

    """ RMS Methods """
    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(f"attempted to get missing private attribute: '{name}'")
        if hasattr(self.model, name):
            return getattr(self.model, name)
        elif hasattr(self.rms, name):
            return getattr(self.rms, name)
        else:
            raise AttributeError(f"no attribute '{name}' is found")

    def get_auxiliary_stats(self):
        return self.rms.get_rms_stats()
    
    def set_auxiliary_stats(self, stats):
        self.rms.set_rms_stats(*stats)

    def save_auxiliary_stats(self):
        """ Save the RMS and the model """
        self.rms.save_rms()

    def restore_auxiliary_stats(self):
        """ Restore the RMS and the model """
        self.rms.restore_rms()


def create_actor(config, model, name='ppo'):
    return PPOActor(config=config, model=model, name=name)