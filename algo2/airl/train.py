import functools

from utility.utils import Every, TempStore
from utility.graph import video_summary
from utility.run import Runner, evaluate
from utility.timer import Timer
from replay.func import create_replay
from algo.ppo.train import main


def get_expert_data(data_path):
    config = dict(
        dir=data_path,
        replay_type='uniform',
        capacity=int(1e6),
        n_steps=1,
        min_size=100,
        batch_size=64,
        has_next_obs=True
    )
    exp_buffer = create_replay(config)
    exp_buffer.load_data()
    print(f'Expert buffer size: {len(exp_buffer)}')
    return exp_buffer
    
def train(agent, env, eval_env, buffer):
    def collect(env, step, reset, next_obs, **kwargs):
        buffer.add(**kwargs)

    step = agent.env_step
    runner = Runner(env, agent, step=step, nsteps=agent.N_STEPS)
    exp_buffer = get_expert_data(buffer.DATA_PATH)

    if step == 0 and agent.is_obs_normalized:
        print('Start to initialize running stats...')
        for _ in range(50):
            runner.run(action_selector=env.random_action, step_fn=collect)
            agent.update_obs_rms(buffer['obs'])
            agent.update_reward_rms(buffer['reward'], buffer['discount'])
            buffer.reset()
        buffer.clear()
        agent.save()

    runner.step = step
    # print("Initial running stats:", *[f'{k:.4g}' for k in agent.get_running_stats() if k])
    to_log = Every(agent.LOG_PERIOD, agent.LOG_PERIOD)
    to_eval = Every(agent.EVAL_PERIOD)
    rt = Timer('run')
    tt = Timer('train')
    et = Timer('eval')
    lt = Timer('log')
    print('Training starts...')
    while step < agent.MAX_STEPS:
        start_env_step = agent.env_step
        agent.before_run(env)
        with rt:
            step = runner.run(step_fn=collect)
        agent.store(fps=(step-start_env_step)/rt.last())
        buffer.reshape_to_sample()
        agent.disc_learn_log(exp_buffer)
        buffer.compute_reward_with_func(agent.compute_reward)
        buffer.reshape_to_store()

        # NOTE: normalizing rewards here may introduce some inconsistency 
        # if normalized rewards is fed as an input to the network.
        # One can reconcile this by moving normalization to collect 
        # or feeding the network with unnormalized rewards.
        # The latter is adopted in our implementation. 
        # However, the following line currently doesn't store
        # a copy of unnormalized rewards
        agent.update_reward_rms(buffer['reward'], buffer['discount'])
        buffer.update('reward', agent.normalize_reward(buffer['reward']), field='all')
        agent.record_last_env_output(runner.env_output)
        value = agent.compute_value()
        buffer.finish(value)

        start_train_step = agent.train_step
        with tt:
            agent.learn_log(step)
        agent.store(tps=(agent.train_step-start_train_step)/tt.last())
        agent.update_obs_rms(buffer['obs'])
        buffer.reset()

        if to_eval(agent.train_step) or step > agent.MAX_STEPS:
            with TempStore(agent.get_states, agent.reset_states):
                with et:
                    eval_score, eval_epslen, video = evaluate(
                        eval_env, agent, n=agent.N_EVAL_EPISODES, 
                        record=agent.RECORD, size=(64, 64))
                if agent.RECORD:
                    video_summary(f'{agent.name}/sim', video, step=step)
                agent.store(
                    eval_score=eval_score, 
                    eval_epslen=eval_epslen)

        if to_log(agent.train_step) and agent.contains_stats('score'):
            with lt:
                agent.store(
                    train_step=agent.train_step,
                    env_time=rt.total(), 
                    train_time=tt.total(),
                    eval_time=et.total())
                agent.log(step)
                agent.save()


main = functools.partial(main, train=train)