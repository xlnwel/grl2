from core.mixin.actor import RMS
from core.module import Actor


class PPOActor(Actor):
    def _post_init(self, config):
        config['rms']['root_dir'] = config['root_dir']
        config['rms']['model_name'] = config['model_name']
        self.rms = RMS(config['rms'])

    """ Calling Methods """
    def _process_input(self, inp: dict, evaluation: bool):
        inp = self.rms.process_obs_with_rms(inp, update_rms=not evaluation)
        return super()._process_input(inp, evaluation)

    def _process_output(self, inp, out, evaluation):
        out = super()._process_output(inp, out, evaluation)
        if not evaluation and self.rms.is_obs_normalized:
            out[1]['obs'] = inp['obs']
        return out

    """ RMS Methods """
    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(f"attempted to get missing private attribute '{name}'")
        if hasattr(self.model, name):
            return getattr(self.model, name)
        elif hasattr(self.rms, name):
            return getattr(self.rms, name)
        else:
            raise AttributeError(f"no attribute '{name}' is found")

    def get_rms_stats(self):
        obs_rms, rew_rms = self.rms.get_rms_stats()
        stats = {}
        if rew_rms:
            stats.update({
                'misc/reward_rms_mean': rew_rms.mean,
                'misc/reward_rms_var': rew_rms.var
            })
        if obs_rms:
            for k, v in obs_rms.items():
                stats.update({
                    f'misc/{k}_rms_mean': v.mean,
                    f'misc/{k}_rms_var': v.var,
                })
        return stats

    def save_auxiliary_stats(self):
        """ Save the RMS and the model """
        self.rms.save_rms()

    def restore_auxiliary_stats(self):
        """ Restore the RMS and the model """
        self.rms.restore_rms()

def create_actor(config, model, name='ppo'):
    return PPOActor(config=config, model=model, name=name)
