import functools
import numpy as np
import tensorflow as tf
from tensorflow.keras.mixed_precision.experimental import global_policy

from core.tf_config import *
from utility.utils import Every
from utility.graph import video_summary
from utility.run import run, evaluate
from env.gym_env import create_env
from replay.func import create_replay
from replay.data_pipline import Dataset, process_with_env
from run import pkg


def train(agent, env, eval_env, replay):
    def collect_and_learn(env, step, **kwargs):
        if env.game_over():
            agent.store(score=env.score(), epslen=env.epslen())
        replay.add(**kwargs)
        agent.learn_log(step)

    start_step = agent.global_steps.numpy() + 1
    step = start_step
    obs = None
    collect_fn = lambda *args, **kwargs: replay.add(**kwargs)
    while not replay.good_to_learn():
        obs, step = run(env, env.random_action, step, obs=obs, 
            fn=collect_fn, nsteps=agent.LOG_INTERVAL)

    to_log = Every(agent.LOG_INTERVAL)
    print('Training starts...')
    while step < int(agent.MAX_STEPS):
        obs, step = run(env, agent, step, obs=obs, 
            fn=collect_and_learn, nsteps=agent.LOG_INTERVAL)
        
        if to_log(step):
            eval_score, eval_epslen, video = evaluate(eval_env, agent, record=False)
            # video_summary(f'{agent.name}/sim', video, step)
            agent.store(eval_score=eval_score, eval_epslen=eval_epslen)
            agent.log(step)
            agent.save(steps=step)


def get_data_format(env, replay_config):
    dtype = global_policy().compute_dtype
    action_dtype = tf.int32 if env.is_action_discrete else dtype
    data_format = dict(
        obs=((None, *env.obs_shape), dtype),
        action=((None, *env.action_shape), action_dtype),
        reward=((None, ), dtype), 
        nth_obs=((None, *env.obs_shape), dtype),
        discount=((None, ), dtype),
    )
    if replay_config['type'].endswith('proportional'):
        data_format['IS_ratio'] = ((None, ), dtype)
        data_format['idxes'] = ((None, ), tf.int32)
    if replay_config.get('n_steps', 1) > 1:
        data_format['steps'] = ((None, ), dtype)

    return data_format

def main(env_config, model_config, agent_config, replay_config):
    silence_tf_logs()
    configure_gpu()

    env = create_env(env_config)
    assert env.n_envs == 1, \
        f'n_envs({env.n_envs}) > 1 is not supported here as it messes with n-step'
    eval_env_config = env_config.copy()
    eval_env = create_env(eval_env_config)

    replay = create_replay(replay_config)

    data_format = get_data_format(env, replay_config)
    print(data_format)
    process = functools.partial(process_with_env, env=env)
    dataset = Dataset(replay, data_format, process_fn=process)

    create_model, Agent = pkg.import_agent(agent_config)
    models = create_model(
        model_config, 
        action_dim=env.action_dim, 
        is_action_discrete=env.is_action_discrete)
    agent = Agent(name='sac', 
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

    # This training process is used for Mujoco tasks, following the same process as OpenAI's spinningup
    # obs = env.reset()
    # epslen = 0
    # to_log = Every(agent.LOG_INTERVAL, start=2*agent.LOG_INTERVAL)
    # for t in range(int(agent.MAX_STEPS)):
    #     if t > 1e4:
    #         action = agent(obs)
    #     else:
    #         action = env.random_action()

    #     nth_obs, reward, done, _ = env.step(action)
    #     epslen += 1
    #     replay.add(obs=obs, action=action, reward=reward, discount=1-done, nth_obs=nth_obs)
    #     obs = nth_obs

    #     if done or epslen == env.max_episode_steps:
    #         agent.store(score=env.score(), epslen=env.epslen())
    #         obs = env.reset()
    #         epslen = 0

    #     if replay.good_to_learn() and t % 50 == 0:
    #         for _ in range(50):
    #             agent.learn_log(t)
    #     if to_log(t):
    #         eval_score, eval_epslen, _ = evaluate(eval_env, agent)

    #         agent.store(eval_score=eval_score, eval_epslen=eval_epslen)
    #         agent.log(step=t)
    #         agent.save(steps=t)
