import gym
from gym.spaces import Box
from gym.wrappers import TimeLimit
import numpy as np

from core.typing import AttrDict
from envs.ma_mujoco_env.multiagent_mujoco.mujoco_multi import MujocoMulti
from envs.utils import *


class MAMujoco(gym.Wrapper):
  def __init__(self, config):
    scenario, agent_conf = config.env_name.split('_')
    if 'env_args' not in config:
      config.env_args = AttrDict()
    config.env_args.scenario = f'{scenario}-v2'
    config.env_args.agent_conf = agent_conf
    config.env_args.episode_limit = config.max_episode_steps

    self.env = MujocoMulti(**config)

    self.action_space = self.env.action_space

    self.single_agent = config.single_agent
    self.use_sample_mask = config.get('use_sample_mask', False)

    if self.single_agent:
      self.n_agents = 1
      self.n_groups = 1
      self.n_units = self.env.n_agents
      self.uid2aid = [0] * self.n_units
      self.uid2gid = [0] * self.n_units
      self.aid2uids = compute_aid2uids(self.uid2aid)
      self.gid2uids = compute_aid2uids(self.uid2gid)
      self.aid2gids = compute_aid2gids(self.uid2aid, self.uid2gid)
    else:
      self.n_agents = 1
      self.n_groups = self.env.n_agents
      self.n_units = self.n_groups
      self.uid2aid = [0] * self.n_units
      self.uid2gid = list(range(self.n_groups))
      self.aid2uids = compute_aid2uids(self.uid2aid)
      self.gid2uids = compute_aid2uids(self.uid2gid)
      self.aid2gids = compute_aid2gids(self.uid2aid, self.uid2gid)

    self.observation_space = [
      Box(low=np.array([-10]*self.n_agents), high=np.array([10]*self.n_agents)) 
      for _ in range(self.n_agents)
    ]

    self.obs_shape = [{
      'obs': (self.env.obs_size, ), 
      'global_state': (self.env.share_obs_size, )
    } for _ in range(self.n_agents)]
    self.obs_dtype = [{
      'obs': np.float32, 
      'global_state': np.float32
    } for _ in range(self.n_agents)]
    
    self.action_shape = [a.shape for a in self.action_space]
    self.action_dim = [a.shape[0] for a in self.action_space]
    self.is_action_discrete = [False for a in self.action_space]
    self.action_dtype = [np.float32 for a in self.action_space]

    self.reward_range = None
    self.metadata = None
    self.max_episode_steps = self.env.episode_limit
    self._score = np.zeros(self.n_agents)
    self._dense_score = np.zeros(self.n_agents)
    self._epslen = 0

  def random_action(self):
    action = np.array([a.sample() for a in self.action_space])
    action = [{'action': action}]
    return action

  def step(self, actions):
    actions = actions[0]['action']
    obs, state, reward, done, _, _ = self.env.step(actions)
    reward = np.reshape(reward, -1)
    done = done[0]
    obs = get_obs(obs, state, self.single_agent)

    self._score += reward[0]
    self._dense_score += reward[0]
    self._epslen += 1

    info = {
      'score': self._score, 
      'dense_score': self._dense_score, 
      'epslen': self._epslen, 
      'game_over': done or self._epslen == self.max_episode_steps
    }

    reward = np.split(reward, self.n_agents)
    if done and self._epslen == self.max_episode_steps:
      done = [np.zeros(self.n_units)] if self.single_agent else \
        [np.zeros(len(self.aid2uids[i])) for i in range(self.n_agents)]
    else:
      done = [np.ones(self.n_units) * done] if self.single_agent else \
        [np.ones(len(self.aid2uids[i])) * done for i in range(self.n_agents)]
    assert len(obs) == self.n_agents, (obs, self.n_agents)
    assert len(reward) == self.n_agents, (reward, self.n_agents)
    assert len(done) == self.n_agents, (done, self.n_agents)
    return obs, reward, done, info

  def reset(self):
    obs, state, _ = self.env.reset()
    obs = get_obs(obs, state, self.single_agent)
    assert len(obs) == self.n_agents, (obs, self.n_agents)

    self._score = np.zeros(self.n_units)
    self._dense_score = np.zeros(self.n_units)
    self._epslen = 0

    return obs

def get_obs(obs, state, single_agent):
  agent_obs = []
  agent_obs.append({'obs': np.stack(obs, -2), 'global_state': np.stack(state, -2)})
  return agent_obs
