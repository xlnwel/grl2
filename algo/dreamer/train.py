import os
import time
import functools
import numpy as np
import tensorflow as tf
from tensorflow.keras.mixed_precision.experimental import global_policy
import ray

from core.tf_config import configure_gpu, configure_precision, silence_tf_logs
from utility.ray_setup import sigint_shutdown_ray
from utility.graph import video_summary
from utility.utils import Every, TempStore
from utility.run import Runner, evaluate
from env.gym_env import create_env
from replay.func import create_replay
from core.dataset import DataFormat, Dataset, process_with_env
from algo.dreamer.env import make_env
from run import pkg


def train(agent, env, eval_env, replay):
    def collect(env, step, **kwargs):
        done = env.already_done()
        if np.any(done):
            if env.n_envs == 1:
                episodes = env.prev_episode
            else:
                episodes = [e.prev_episode for e, d in zip(env.envs, done) if d]
            replay.merge(episodes)
    _, step = replay.count_episodes()
    step = max(agent.global_steps.numpy(), step)

    nsteps = agent.TRAIN_INTERVAL
    runner = Runner(env, agent, step=step)
    while not replay.good_to_learn():
        step = runner.run(action_selector=env.random_action, step_fn=collect)
        
    to_log = Every(agent.LOG_INTERVAL)
    to_eval = Every(agent.EVAL_INTERVAL)
    print('Training starts...')
    start_step = step
    start_t = time.time()
    while step < int(agent.MAX_STEPS):
        agent.learn_log(step)
        step = runner.run(step_fn=collect, nsteps=nsteps)
        duration = time.time() - start_t
        if to_eval(step):
            with TempStore(agent.get_states, agent.reset_states):
                score, epslen, video = evaluate(eval_env, agent, record=True, size=(64, 64))
                video_summary(f'{agent.name}/sim', video, step=step)
                agent.store(eval_score=score, eval_epslen=epslen)
            
        if to_log(step):
            agent.store(fps=(step-start_step)/duration, duration=duration)
            agent.log(step)
            agent.save(steps=step)

            start_step = step
            start_t = time.time()

def get_data_format(env, batch_size, sample_size=None):
    dtype = global_policy().compute_dtype
    data_format = dict(
        obs=DataFormat((batch_size, sample_size, *env.obs_shape), dtype),
        action=DataFormat((batch_size, sample_size, *env.action_shape), dtype),
        reward=DataFormat((batch_size, sample_size), dtype), 
        discount=DataFormat((batch_size, sample_size), dtype),
    )
    return data_format

def main(env_config, model_config, agent_config, replay_config):
    silence_tf_logs()
    configure_gpu()
    configure_precision(env_config['precision'])

    use_ray = env_config.get('n_workers', 0) > 1
    if use_ray:
        ray.init()
        sigint_shutdown_ray()

    env = create_env(env_config, make_env, force_envvec=True)
    eval_env_config = env_config.copy()
    eval_env_config['n_envs'] = 1
    eval_env_config['n_workers'] = 1
    eval_env_config['log_episode'] = False
    if 'reward_hack' in eval_env_config:
        del eval_env_config['reward_hack']
    eval_env = create_env(eval_env_config, make_env)

    replay_config['dir'] = agent_config['root_dir'].replace('logs', 'data')
    replay = create_replay(replay_config)
    replay.load_data()
    data_format = get_data_format(env, agent_config['batch_size'], agent_config['sample_size'])
    process = functools.partial(process_with_env, env=env, obs_range=[-.5, .5])
    dataset = Dataset(replay, data_format, process)

    create_model, Agent = pkg.import_agent(agent_config)
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

    train(agent, env, eval_env, replay)
