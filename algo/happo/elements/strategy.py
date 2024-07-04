from functools import partial
from typing import List
import numpy as np
import jax
import jax.numpy as jnp

from core.typing import AttrDict, dict2AttrDict
from jx.elements.strategy import Strategy as StrategyBase, create_strategy
from core.mixin.strategy import Memory as MemoryBase
from envs.typing import EnvOutput
from tools.display import print_dict_info


class Memory(MemoryBase):
  def add_memory_state_to_input(self, 
      inps: List, resets: List, states: List=None):
    if states is None and self._state is None:
      self._state = self.model.get_initial_state(
        next(iter(inps[0].values())).shape[0])

    if states is None:
      states = self._state
    if states is None:
      return inps  # no memory is maintained

    assert len(inps) == len(states) == len(resets), (len(inps), len(states), len(resets))
    for inp, state, reset in zip(inps, states, resets):
      reset_exp = np.expand_dims(reset, -1)
      inp.state = jax.tree_util.tree_map(lambda x: x*(1-reset_exp), state)
      inp.state_reset = reset
    
    return inps

  def apply_reset_to_state(self, states: List[AttrDict], resets: List):
    if states is None:
      return
    for i, reset in enumerate(resets):
      reset = jnp.expand_dims(reset, -1)
      states[i] = jax.tree_util.tree_map(lambda x: x*(1-reset), states[i])
    return states


class Strategy(StrategyBase):
  def _setup_memory_cls(self):
    self.memory_cls = Memory

  def _post_init(self):
    self.gid2uids = self.model.gid2uids

  def _prepare_input_to_actor(self, env_output):
    if isinstance(env_output.obs, list):
      assert len(env_output.obs) == 1, len(env_output.obs)
      env_output = EnvOutput(*[x[0] for x in env_output])
    inps = [env_output.obs.slice(indices=uids, axis=1) for uids in self.gid2uids]
    resets = [env_output.reset[:, uids] for uids in self.gid2uids]
    inps = self._memory.add_memory_state_to_input(inps, resets)

    return inps

  def _record_output(self, out):
    state = out[-1]
    self._memory.set_states(state)
    return out

  def compute_value(self, env_output, states=None):
    if isinstance(env_output.obs, list):
      assert len(env_output.obs) == 1, len(env_output.obs)
      env_output = EnvOutput(*[x[0] for x in env_output])
    inps = [env_output.obs.slice(indices=uids, axis=1) for uids in self.gid2uids]
    resets = [env_output.reset[:, uids] for uids in self.gid2uids]
    inps = self._memory.add_memory_state_to_input(inps, resets, states)
    value = self.actor.compute_value(inps)

    return value


create_strategy = partial(create_strategy, strategy_cls=Strategy)
