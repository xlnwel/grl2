from functools import partial

from algo.ma_common.train import *
from algo.lka_common.train import eval_ego_and_lka, lka_optimize, env_run
from algo.masac.train import lka_env_run


def train(
    agent, 
    runner: Runner, 
    routine_config, 
    # env_run=env_run, 
    # ego_opt=ego_optimize
):
    MODEL_EVAL_STEPS = runner.env.max_episode_steps
    do_logging(f'Model evaluation steps: {MODEL_EVAL_STEPS}')
    do_logging('Training starts...')
    env_step = agent.get_env_step()
    to_record = Every(
        routine_config.LOG_PERIOD, 
        start=env_step, 
        init_next=env_step != 0, 
        final=routine_config.MAX_STEPS
    )
    runner.run(MODEL_EVAL_STEPS, agent, [], collect_data=False)

    while env_step < routine_config.MAX_STEPS:
        lka_env_run(agent, runner, routine_config, lka_aids=[])
        lka_optimize(agent)
        env_step = env_run(agent, runner, routine_config, lka_aids=None)
        train_step = ego_optimize(agent)
        time2record = to_record(env_step)
        
        if time2record:
            eval_ego_and_lka(agent, runner, routine_config)
            save(agent, None)
            log(agent, None, env_step, train_step, {})


main = partial(main, train=train)