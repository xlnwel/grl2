from core.typing import AttrDict, dict2AttrDict
from core.elements.model import Model


class Buffer:
  def __init__(
    self, 
    config: AttrDict,
    env_stats: AttrDict, 
    model: Model,
    aid: int=0, 
  ):
    self.config = dict2AttrDict(config, to_copy=True)
    self.env_stats = dict2AttrDict(env_stats, to_copy=True)
    self.model = model
    self.aid = aid

    self.obs_keys = env_stats.obs_keys[self.aid]
    self.state_keys, self.state_type, \
      self.sample_keys, self.sample_size = \
        extract_sampling_keys(self.config, env_stats, model, aid)

  def type(self):
    return self.config.type

  def reset(self):
    raise NotImplementedError
  
  def sample(self):
    raise NotImplementedError

  def collect(self, idxes=None, **data):
    data = self._prepare_data(**data)
    self.add(idxes=idxes, **data)
  
  def add(self, **data):
    raise NotImplementedError

  """ Implementation """
  def _prepare_data(self, obs, next_obs, **data):
    for k, v in obs.items():
      if k not in data:
        data[k] = v
    for k, v in next_obs.items():
      data[f'next_{k}'] = v
    return data


def extract_sampling_keys(
  config: AttrDict, 
  env_stats: AttrDict, 
  model: Model, 
  aid: int
):
  if model is None:
    state_keys = None
    state_type = None
  else:
    state_keys = model.state_keys
    state_type = model.state_type
  sample_keys = config.sample_keys
  sample_size = config.get('sample_size', config.n_steps)
  sample_keys = set(sample_keys)
  if state_keys is None:
    if 'state' in sample_keys:
      sample_keys.remove('state')
    if 'state_reset' in sample_keys:
      sample_keys.remove('state_reset')
  else:
    sample_keys.add('state')
    sample_keys.add('state_reset')
  obs_keys = env_stats.obs_keys[aid]
  for k in obs_keys:
    sample_keys.add(k)
  for k in obs_keys:
    sample_keys.add(f'next_{k}')
  if (env_stats.use_action_mask[aid] if isinstance(env_stats.use_action_mask, (list, tuple)) else env_stats.use_action_mask):
    sample_keys.add('action_mask')
  elif 'action_mask' in sample_keys:
    sample_keys.remove('action_mask')
  if (env_stats.use_sample_mask[aid] if isinstance(env_stats.use_sample_mask, (list, tuple)) else env_stats.use_sample_mask):
    sample_keys.add('sample_mask')
  elif 'sample_mask' in sample_keys:
    sample_keys.remove('sample_mask')
  return state_keys, state_type, sample_keys, sample_size
