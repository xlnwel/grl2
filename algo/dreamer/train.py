import os
import time
import functools
import numpy as np
import tensorflow as tf
from tensorflow.keras.mixed_precision.experimental import global_policy
import ray

from core.tf_config import configure_gpu, configure_precision, silence_tf_logs
from utility.signal import sigint_shutdown_ray
from utility.graph import video_summary
from utility.utils import Every
from env.gym_env import create_env
from replay.func import create_replay
from replay.data_pipline import DataFormat, Dataset, process_with_env
from algo.sac.run import evaluate
from algo.dreamer.agent import Agent
from algo.dreamer.nn import create_model
from algo.dreamer.env import make_env


def run(env, agent, obs=None, already_done=None, 
        fn=None, nsteps=0, evaluation=False):
    if obs is None:
        obs = env.reset()
    if already_done is None:
        already_done = env.already_done()
    nsteps = nsteps or env.max_episode_steps
    for i in range(env.n_ar, nsteps + env.n_ar, env.n_ar):
        action = agent(obs, already_done, deterministic=evaluation)
        obs, reward, done, info = env.step(action)
        already_done = env.already_done()
        if fn:
            fn(already_done, info)

    return obs, already_done, i * env.n_envs

def train(agent, env, eval_env, replay):
    def collect_log(already_done, info):
        if already_done.any():
            episodes, scores, epslens = [], [], []
            for i, d in enumerate(already_done):
                if d:
                    eps = info[i]['episode']
                    episodes.append(eps)
                    scores.append(np.sum(eps['reward']))
                    epslens.append(env.n_ar*(eps['reward'].size-1))
            agent.store(score=scores, epslen=epslens)
            replay.merge(episodes)

    _, step = replay.count_episodes()
    step = max(agent.global_steps.numpy(), step)

    nsteps = agent.TRAIN_INTERVAL
    obs, already_done = None, None
    while not replay.good_to_learn():
        obs, already_done, n = run(
            env, env.random_action, obs, already_done, collect_log)
        step += n
        
    to_log = Every(agent.LOG_INTERVAL, start=step)
    to_eval = Every(agent.EVAL_INTERVAL, start=step)
    print('Training started...')
    start_step = step
    start_t = time.time()
    while step < int(agent.MAX_STEPS):
        agent.learn_log(step)
        obs, already_done, n = run(
            env, agent, obs, already_done, collect_log, nsteps)
        step += n
        if to_eval(step):
            train_state, train_action = agent.retrieve_states()

            score, epslen = evaluate(eval_env, agent)
            video_summary('dreamer/sim', eval_env.prev_episode['obs'][None], step)
            agent.store(eval_score=score, eval_epslen=epslen)
            
            agent.reset_states(train_state, train_action)
        if to_log(step):
            agent.store(fps=(step-start_step)/(time.time()-start_t))
            agent.log(step)
            agent.save(steps=step)

            start_step = step
            start_t = time.time()

def get_data_format(env, batch_size, batch_len=None):
    dtype = global_policy().compute_dtype
    data_format = dict(
        obs=DataFormat((batch_size, batch_len, *env.obs_shape), dtype),
        action=DataFormat((batch_size, batch_len, *env.action_shape), dtype),
        reward=DataFormat((batch_size, batch_len), dtype), 
        discount=DataFormat((batch_size, batch_len), dtype),
    )
    return data_format

def main(env_config, model_config, agent_config, 
        replay_config, restore=False, render=False):
    silence_tf_logs()
    configure_gpu()
    configure_precision(agent_config['precision'])

    use_ray = env_config.get('n_workers', 0) > 1
    if use_ray:
        ray.init()
        sigint_shutdown_ray()

    env = create_env(env_config, make_env, force_envvec=True)
    eval_env_config = env_config.copy()
    eval_env_config['auto_reset'] = False
    eval_env_config['n_envs'] = 1
    eval_env_config['n_workers'] = 1
    eval_env = create_env(eval_env_config, make_env, force_envvec=True)

    replay_config['dir'] = agent_config['root_dir'].replace('logs', 'data')
    replay = create_replay(replay_config)
    replay.load_data()
    data_format = get_data_format(env, agent_config['batch_size'], agent_config['batch_len'])
    print(data_format)
    process = functools.partial(
        process_with_env, env=env, obs_range=agent_config['obs_range'])
    dataset = Dataset(replay, data_format, process)

    models = create_model(
        model_config, 
        obs_shape=env.obs_shape,
        action_dim=env.action_dim,
        is_action_discrete=env.is_action_discrete
    )

    agent = Agent(
        name='dreamer',
        config=agent_config,
        models=models, 
        dataset=dataset,
        env=env)

    agent.save_config(dict(
        env=env_config,
        model=model_config,
        agent=agent_config,
        replay=replay_config
    ))

    if restore:
        agent.restore()

    train(agent, env, eval_env, replay)
