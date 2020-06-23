import time
import gym
import numpy as np

from procgen.env import ENV_NAMES as VALID_ENV_NAMES
from env import wrappers as W
from env import baselines as B 

def make_procgen_env(config):
    gray_scale = config.setdefault('gray_scale', False)
    frame_skip = config.setdefault('frame_skip', 1)
    frame_stack = config.setdefault('frame_stack', 1)
    np_obs = config.setdefault('np_obs', False)
    env = Procgen(config)
    if gray_scale:
        env = W.GrayScale(env)
    if frame_skip > 1:
        if gray_scale:
            env = B.MaxAndSkipEnv(env, frame_skip=frame_skip)
        else:
            env = W.FrameSkip(env, frame_skip=frame_skip)
    if frame_stack > 1:
        env = B.FrameStack(env, frame_stack, np_obs)
    config.setdefault('max_episode_steps', env.spec.max_episode_steps)
    
    return env

class Procgen(gym.Env):
    """
    Procgen Wrapper file
    """
    def __init__(self, config):
        self._default_config = {
            "num_levels" : 0,  # The number of unique levels that can be generated. Set to 0 to use unlimited levels.
            "name" : "coinrun",  # Name of environment, or comma-separate list of environment names to instantiate as each env in the VecEnv
            "start_level" : 0,  # The lowest seed that will be used to generated levels. 'start_level' and 'num_levels' fully specify the set of possible levels
            "paint_vel_info" : False,  # Paint player velocity info in the top left corner. Only supported by certain games.
            "use_generated_assets" : False,  # Use randomly generated assets in place of human designed assets
            "center_agent" : True,  # Determines whether observations are centered on the agent or display the full level. Override at your own risk.
            "use_sequential_levels" : False,  # When you reach the end of a level, the episode is ended and a new level is selected. If use_sequential_levels is set to True, reaching the end of a level does not end the episode, and the seed for the new level is derived from the current level seed. If you combine this with start_level=<some seed> and num_levels=1, you can have a single linear series of levels similar to a gym-retro or ALE game.
            "distribution_mode" : "easy"  # What variant of the levels to use, the options are "easy", "hard", "extreme", "memory", "exploration". All games support "easy" and "hard", while other options are game-specific. The default is "hard". Switching to "easy" will reduce the number of timesteps required to solve each game and is useful for testing or when working with limited compute resources. NOTE : During the evaluation phase (rollout), this will always be overriden to "easy"
        }
        self.config = self._default_config
        self.config.update({
            k: config[k] for k in 
            config.keys() & self.config.keys()})

        name = self.config.pop("name")
        self.name = name.split('_', 1)[-1]

        assert self.name in VALID_ENV_NAMES, self.name

        env = gym.make(f"procgen:procgen-{self.name}-v0", **self.config)
        self.env = env
        # Enable video recording features
        self.metadata = self.env.metadata

        self.action_space = self.env.action_space
        self.observation_space = self.env.observation_space
        self._game_over = True
        self._obs = None

    def reset(self):
        self._game_over = False
        obs = self.env.reset()
        self._obs = obs
        return obs

    def step(self, action):
        obs, rew, done, info = self.env.step(action)
        self._game_over = done
        self._obs = obs
        return obs, rew, done, info

    def render(self, mode="human"):
        return self.env.render(mode=mode)

    def close(self):
        return self.env.close()

    def seed(self, seed=None):
        return self.env.seed()

    def __repr__(self):
        return self.env.__repr()

    @property
    def spec(self):
        return self.env.spec

    def get_screen(self):
        return self._obs

    def game_over(self):
        return self._game_over
    
    def set_game_over(self):
        return self._game_over

bounds = dict(
    coinrun=[5, 10],
)

def compute_normalized_score(score, name='coinrun'):
    return (score - bounds[0]) / (bounds[1] - bounds[0])