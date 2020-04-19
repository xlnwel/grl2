""" Implementation of single process environment """
import itertools
import numpy as np
import gym
import tensorflow as tf
import ray
import cv2

from utility.utils import isscalar
from env.wrappers import *
from env.atari import make_atari_env


def _make_env(config):
    config = config.copy()
    if 'atari' in config['name'].lower():
        # for atari games, we expect to following name convention 'atari_name'
        _, config['name'] = config['name'].split('_', 1)
        config['max_episode_steps'] = max_episode_steps = 108000    # 30min
        env = make_atari_env(config)
    else:
        env = gym.make(config['name']).env
        max_episode_steps = config.get('max_episode_steps', env.spec.max_episode_steps)
        if config.get('frame_skip'):
            env = FrameSkip(env, config['frame_skip'])
    env = EnvStats(env, max_episode_steps,
                    precision=config.get('precision', 32), 
                    timeout_done=config.get('timeout_done', False))
    if config.get('log_episode'):
        env = LogEpisode(env)

    return env

def create_env(config, env_fn=_make_env, force_envvec=False):
    if force_envvec and config.get('n_workers', 1) <= 1:
        EnvType = EnvVec
    else:
        EnvType = EnvVec if config.get('n_envs', 1) > 1 else Env
    if config.get('n_workers', 1) <= 1:
        if EnvType == EnvVec and config.get('efficient_envvec', False):
            EnvType = EfficientEnvVec
        return EnvType(config, env_fn)
    else:
        return RayEnvVec(EnvType, config, env_fn)


class EnvVecBase:
    def _convert_batch_obs(self, obs):
        if isinstance(obs[0], np.ndarray):
            obs = np.reshape(obs, [-1, *self.obs_shape])
        else:
            obs = list(obs)
        return obs


class Env:
    def __init__(self, config, env_fn=_make_env):
        self.env = env_fn(config)
        self.name = config['name']
        self.max_episode_steps = self.env.max_episode_steps


    def __getattr__(self, name):
        return getattr(self.env, name)

    def reset(self, idxes=None):
        assert idxes is None or idxes == 0, idxes
        return self.env.reset()

    @property
    def n_envs(self):
        return 1

    def random_action(self, *args, **kwargs):
        action = self.env.action_space.sample()
        return action
        
    def step(self, action, **kwargs):
        return self.env.step(action, **kwargs)

    """ the following code is needed for ray """
    def mask(self):
        return self.env.mask()
    
    def score(self, **kwargs):
        return self.env.score(**kwargs)

    def epslen(self, **kwargs):
        return self.env.epslen(**kwargs)

    def already_done(self):
        return self.env.already_done()

    def game_over(self):
        return self.env.game_over()

    def get_screen(self, size=None):
        if 'atari' in self.name:
            img = self.env.get_screen()
        else:
            img = self.env.render(mode='rgb_array')
        
        if size is not None:
            img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
            
        return img

class EnvVec(EnvVecBase):
    def __init__(self, config, env_fn=_make_env):
        self.n_envs = n_envs = config['n_envs']
        self.name = config['name']
        self.envs = [env_fn(config) for i in range(n_envs)]
        self.env = self.envs[0]
        if 'seed' in config:
            [hasattr(env, 'seed') and env.seed(config['seed'] + i) 
                for i, env in enumerate(self.envs)]
        self.max_episode_steps = self.env.max_episode_steps

    def __getattr__(self, name):
        return getattr(self.env, name)

    def random_action(self, *args, **kwargs):
        return np.array([env.action_space.sample() for env in self.envs], copy=False)

    def reset(self, idxes=None):
        if idxes is None:
            obs = [env.reset() for env in self.envs]
            return self._convert_batch_obs(obs)
        else:
            if not isinstance(idxes, (list, tuple)):
                idxes = [idxes]
            return [self.envs[i].reset() for i in idxes]
    
    def step(self, actions, **kwargs):
        obs, reward, done, info = _envvec_step(self.envs, actions, **kwargs)

        return (self._convert_batch_obs(obs), 
                np.array(reward, dtype=np.float32), 
                np.array(done, dtype=np.bool), 
                info)

    def mask(self):
        return np.array([env.mask() for env in self.envs], dtype=np.bool)

    def score(self, idxes=None):
        if idxes is None:
            return np.array([env.score() for env in self.envs])
        else:
            if not isinstance(idxes, (list, tuple)):
                idxes = [idxes]
            return [self.envs[i].score() for i in idxes]

    def epslen(self, idxes=None):
        if idxes is None:
            return np.array([env.epslen() for env in self.envs])
        else:
            if not isinstance(idxes, (list, tuple)):
                idxes = [idxes]
            return [self.envs[i].epslen() for i in idxes]

    def already_done(self):
        return np.array([env.already_done() for env in self.envs], dtype=np.bool)

    def game_over(self):
        return np.array([env.game_over() for env in self.envs], dtype=np.bool)
        
    def get_screen(self, size=None):
        if 'atari' in self.name:
            imgs = np.array([env.get_screen() for env in self.envs], copy=False)
        else:
            imgs = np.array([env.render(mode='rgb_array') for env in self.envs],
                            copy=False)

        if size is not None:
            imgs = np.array([cv2.resize(i, size, interpolation=cv2.INTER_AREA) for i in imgs])
        
        return imgs

class EfficientEnvVec(EnvVec):
    """ Designed for evaluation only """
    def reset(self, idxes=None):
        self.valid_envs = self.envs
        return super().reset(idxes)

    def random_action(self):
        return [env.action_space.sample() for env in self.valid_envs]
        
    def step(self, actions, **kwargs):
        if len(self.valid_envs) == 1:
            obs, reward, done, info = self.valid_envs[0].step(actions, **kwargs)
        else:
            obs, reward, done, info = _envvec_step(self.valid_envs, actions, **kwargs)
            
            self.valid_envs, obs, reward, done, info = \
                zip(*[(e, o, r, d, i)
                    for e, o, r, d, i in zip(self.envs, obs, reward, done, info) 
                    if not e.already_done()])
        
        return (self._convert_batch_obs(obs), 
                np.array(reward, dtype=np.float32), 
                np.array(done, dtype=np.bool), 
                info)


class RayEnvVec(EnvVecBase):
    def __init__(self, EnvType, config, env_fn=_make_env):
        self.name = config['name']
        self.n_workers= config['n_workers']
        self.envsperworker = config['n_envs']
        self.n_envs = self.envsperworker * self.n_workers
        RayEnvType = ray.remote(EnvType)
        # leave the name "envs" for consistency, albeit workers seems more appropriate
        if 'seed' in config:
            self.envs = [config.update({'seed': 100*i}) or RayEnvType.remote(config, env_fn) 
                    for i in range(self.n_workers)]
        else:
            self.envs = [RayEnvType.remote(config, env_fn) 
                    for i in range(self.n_workers)]

        self.env = EnvType(config, env_fn)
        self.max_episode_steps = self.env.max_episode_steps

    def __getattr__(self, name):
        return getattr(self.env, name)

    def reset(self, idxes=None):
        if idxes is None:
            obs = tf.nest.flatten(ray.get([env.reset.remote() for env in self.envs]))
            return self._convert_batch_obs(obs)
        else:
            new_idxes = [[] for _ in range(self.n_workers)]
            [new_idxes[i // self.n_workers].append(i % self.n_workers) for i in idxes]
            obs = tf.nest.flatten(ray.get([self.envs[i].reset.remote(j) for i, j in enumerate(new_idxes)]))
            return obs

    def random_action(self, *args, **kwargs):
        return np.reshape(ray.get([env.random_action.remote() for env in self.envs]), 
                          (self.n_envs, *self.action_shape))

    def step(self, actions, **kwargs):
        actions = np.squeeze(actions.reshape(self.n_workers, self.envsperworker, *self.action_shape))
        if kwargs:
            kwargs = dict([(k, np.squeeze(v.reshape(self.n_workers, self.envsperworker, -1))) for k, v in kwargs.items()])
            kwargs = [dict(v) for v in zip(*[itertools.product([k], v) for k, v in kwargs.items()])]
            obs, reward, done, info = zip(*ray.get([env.step.remote(a, **kw) for env, a, kw in zip(self.envs, actions, kwargs)]))
        else:
            obs, reward, done, info = zip(*ray.get([env.step.remote(a) for env, a in zip(self.envs, actions)]))
        if not isinstance(self.env, Env):
            info_lists = info
            info = []
            for i in info_lists:
                info += i

        obs = tf.nest.flatten(obs)
        return (self._convert_batch_obs(obs),
                np.reshape(reward, self.n_envs).astype(np.float32), 
                np.reshape(done, self.n_envs).astype(bool),
                info)

    def mask(self):
        """ Get mask at the current step. Should only be called after self.step """
        return np.reshape(ray.get([env.mask.remote() for env in self.envs]), self.n_envs)

    def score(self, idxes=None):
        if idxes is None:
            return np.reshape(ray.get([env.score.remote() for env in self.envs]), self.n_envs)
        else:
            new_idxes = [[] for _ in range(self.n_workers)]
            [new_idxes[i // self.n_workers].append(i % self.n_workers) for i in idxes]
            return tf.nest.flatten([self.envs[i].score(j) for i, j in enumerate(new_idxes)])

    def epslen(self, idxes=None):
        if idxes is None:
            return np.reshape(ray.get([env.epslen.remote() for env in self.envs]), self.n_envs)
        else:
            new_idxes = [[] for _ in range(self.n_workers)]
            [new_idxes[i // self.n_workers].append(i % self.n_workers) for i in idxes]
            return tf.nest.flatten([self.envs[i].epslen(j) for i, j in enumerate(new_idxes)])

    def already_done(self):
        return np.reshape(ray.get([env.already_done.remote() for env in self.envs]), self.n_envs)

    def close(self):
        del self
    
def _envvec_step(envvec, actions, **kwargs):
    if kwargs:
        for k, v in kwargs.items():
            if isscalar(v):
                kwargs[k] = np.tile(v, actions.shape[0])
        kwargs = [dict(v) for v in zip(*[itertools.product([k], v) for k, v in kwargs.items()])]
        return zip(*[env.step(a, **kw) for env, a, kw in zip(envvec, actions, kwargs)])
    else:
        return zip(*[env.step(a) for env, a in zip(envvec, actions)])


if __name__ == '__main__':
    # performance test
    config = dict(
        name='atari_breakout',
        seed=0,
        life_done=True,
    )
    from utility.run import run
    import matplotlib.pyplot as plt
    env = create_env(config)
    obs = env.reset()
    def fn(env, step, **kwargs):
        assert step % env.frame_skip == 0
    run(env, env.random_action, 0, fn=fn, nsteps=10000)
    