import numpy as np
import tensorflow as tf
from tensorflow.keras.mixed_precision import experimental as prec

from core.tf_config import configure_gpu, configure_precision
from utility.utils import set_global_seed, Every
from env.gym_env import create_gym_env
from replay.func import create_replay
from replay.data_pipline import DataFormat, Dataset
from algo.run import run, random_sampling
from algo.sac.agent import Agent
from algo.sac.nn import create_model


def train(agent, env, replay):
    def collect_and_learn(step, **kwargs):
        replay.add(**kwargs)
        agent.learn_log(step)

    eval_env = create_gym_env(dict(
        name=env.name, 
        video_path='video',
        log_video=False,
        n_workers=1,
        n_envs=10,
        effective_envvec=True,
        seed=0,
    ))
    start_step = agent.global_steps.numpy() + 1

    print('Training started...')
    step = start_step
    should_log = Every(agent.LOG_INTERVAL)
    while step < int(agent.MAX_STEPS):
        agent.set_summary_step(step)
        score, epslen = run(env, agent.actor, fn=collect_and_learn, 
            timer=agent.TIMER, step=step)
        agent.store(score=env.get_score(), epslen=env.get_epslen())
        step += epslen
        
        if should_log(step):
            agent.save(steps=step)

            eval_score, eval_epslen = run(eval_env, agent.actor, 
                evaluation=True, timer=agent.TIMER, name='eval')
            
            agent.store(eval_score=eval_score, eval_epslen=eval_epslen)
            agent.store(**agent.get_value('score', mean=True, std=True, min=True, max=True))
            agent.store(**agent.get_value('epslen', mean=True, std=True, min=True, max=True))
            agent.store(**agent.get_value('eval_score', mean=True, std=True, min=True, max=True))
            agent.store(**agent.get_value('eval_epslen', mean=True, std=True, min=True, max=True))

            agent.log(step)


def main(env_config, model_config, agent_config, replay_config, restore=False, render=False):
    set_global_seed(seed=env_config['seed'], tf=tf)
    # tf.debugging.set_log_device_placement(True)
    configure_gpu()
    configure_precision(agent_config.get('precision', 32))

    env = create_gym_env(env_config)
    replay = create_replay(replay_config)

    data_format = dict(
        obs=DataFormat((None, *env.obs_shape), env.obs_dtype),
        action=DataFormat((None, *env.action_shape), env.action_dtype),
        reward=DataFormat((None, ), tf.float32), 
        next_obs=DataFormat((None, *env.obs_shape), env.obs_dtype),
        done=DataFormat((None, ), tf.float32),
    )
    if replay_config.get('n_steps', 1) > 1:
        data_format['steps'] = DataFormat((None, ), tf.float32)
    dataset = Dataset(replay, data_format)

    models = create_model(
        model_config, 
        obs_shape=env.obs_shape, 
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

    if restore:
        agent.restore()
        collect_fn = lambda **kwargs: replay.add(**kwargs)      
        while not replay.good_to_learn():
            run(env, agent.actor, collect_fn)
    else:
        random_sampling(env, replay)

    train(agent, env, replay)

    # This training process is used for Mujoco tasks, following the same process as OpenAI's spinningup
    # obs = env.reset()
    # eval_env = create_gym_env(dict(
    #     name=env.name, 
    #     video_path='video',
    #     log_video=False,
    #     n_workers=1,
    #     n_envs=10,
    #     effective_envvec=True,
    #     seed=0,
    # ))

    # epslen = 0
    # for t in range(int(agent.MAX_STEPS)):
    #     if t > 1e4:
    #         action, _ = agent.action(obs)
    #     else:
    #         action = env.random_action()

    #     next_obs, reward, done, _ = env.step(action)
    #     epslen += 1
    #     done = False if epslen == env.max_episode_steps else done
    #     replay.add(obs=obs, action=action, reward=reward, done=done, next_obs=next_obs)
    #     obs = next_obs

    #     if done or epslen == env.max_episode_steps:
    #         agent.store(score=env.get_score(), epslen=env.get_epslen())
    #         obs = env.reset()
    #         epslen = 0

    #     if replay.good_to_learn() and t % 50 == 0:
    #         for _ in range(50):
    #             agent.learn_log()
    #     if (t + 1) % 4000 == 0:
    #         eval_score, eval_epslen = run(eval_env, agent.actor, evaluation=True)

    #         agent.store(eval_score=eval_score, eval_epslen=eval_epslen)
    #         agent.store(**agent.get_value('score', mean=True, std=True, min=True, max=True))
    #         agent.store(**agent.get_value('epslen', mean=True, std=True, min=True, max=True))
    #         agent.store(**agent.get_value('eval_score', mean=True, std=True, min=True, max=True))
    #         agent.store(**agent.get_value('eval_epslen', mean=True, std=True, min=True, max=True))
            
    #         agent.log(step=t)
