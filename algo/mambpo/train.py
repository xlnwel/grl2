from core.typing import modelpath2outdir
from tools.store import TempStore
from replay.dual import DualReplay, PRIMAL_REPLAY
from algo.masac.train import *
from algo.mambpo.run import *


@timeit
def model_train(model):
    model.train_record()


@timeit
def lookahead_run(agent, model, routine_config, rng):
    def get_agent_states():
        state = agent.get_states()
        # we collect lookahead data into the secondary replay
        if isinstance(agent.buffer, DualReplay):
            agent.buffer.set_default_replay(routine_config.lookahead_replay)
        return state
    
    def set_agent_states(states):
        agent.set_states(states)
        if isinstance(agent.buffer, DualReplay):
            agent.buffer.set_default_replay(PRIMAL_REPLAY)

    # train lookahead agent
    with TempStore(get_agent_states, set_agent_states):
        run_on_model(
            model, agent, routine_config, rng)


@timeit
def lookahead_optimize(agent):
    agent.lookahead_train()


@timeit
def lookahead_train(agent, model, routine_config, 
        n_runs, run_fn, opt_fn, rng):
    if not model.trainer.is_trust_worthy() \
        or not model.buffer.ready_to_sample():
        return
    assert n_runs >= 0, n_runs
    for _ in range(n_runs):
        run_fn(agent, model, routine_config, rng)
        opt_fn(agent)


def update_config(config, model_config):
    config.buffer.primal_replay.model_norm_obs = model_config.buffer.model_norm_obs


def train(
    agent, 
    model, 
    runner, 
    routine_config,
    model_routine_config,
    lka_run_fn=lookahead_run, 
    lka_opt_fn=lookahead_optimize, 
    lka_train_fn=lookahead_train, 
    ego_run_fn=ego_run, 
    ego_opt_fn=ego_optimize, 
    ego_train_fn=ego_train, 
    model_train_fn=model_train
):
    MODEL_EVAL_STEPS = runner.env.max_episode_steps
    print('Model evaluation steps:', MODEL_EVAL_STEPS)
    do_logging('Training starts...')
    env_step = agent.get_env_step()
    to_record = Every(
        routine_config.LOG_PERIOD, 
        start=env_step, 
        init_next=env_step != 0, 
        final=routine_config.MAX_STEPS
    )
    runner.run(MODEL_EVAL_STEPS, agent, None, None, [])
    rng = agent.model.rng

    while env_step < routine_config.MAX_STEPS:
        rng, lka_rng = jax.random.split(rng, 2)
        errors = AttrDict()
        env_step = ego_run_fn(agent, runner, routine_config)
        time2record = to_record(env_step)
        
        model_train_fn(model)
        if routine_config.quantify_model_errors and time2record:
            errors.train = quantify_model_errors(
                agent, model, runner.env_config(), MODEL_EVAL_STEPS, [])

        if model_routine_config.model_warm_up and env_step < model_routine_config.model_warm_up_steps:
            pass
        else:
            lka_train_fn(
                agent, 
                model, 
                routine_config, 
                n_runs=routine_config.n_lookahead_steps, 
                run_fn=lka_run_fn, 
                opt_fn=lka_opt_fn, 
                rng=lka_rng
            )

        train_step = ego_opt_fn(agent)
        if routine_config.quantify_model_errors and time2record:
            errors.ego = quantify_model_errors(
                agent, model, runner.env_config(), MODEL_EVAL_STEPS, [])

        if time2record:
            evaluate(agent, model, runner, env_step, routine_config)
            if routine_config.quantify_model_errors:
                outdir = modelpath2outdir(agent.get_model_path())
                log_model_errors(errors, outdir, env_step)
            save(agent, model)
            log(agent, model, env_step, train_step, errors)


def main(configs, train=train):
    config, model_config = configs[0], configs[-1]
    update_config(config, model_config)
    seed = config.get('seed')
    set_seed(seed)

    configure_gpu()
    use_ray = config.env.get('n_runners', 1) > 1
    if use_ray:
        from tools.ray_setup import sigint_shutdown_ray
        ray.init(num_cpus=config.env.n_runners)
        sigint_shutdown_ray()

    runner = Runner(config.env)

    # load agent
    env_stats = runner.env_stats()
    env_stats.n_envs = config.env.n_runners * config.env.n_envs
    print_dict(env_stats)

    # build agents
    agent = build_agent(config, env_stats)
    # load model
    model = build_model(config, model_config, env_stats)
    model.change_buffer(agent.buffer)
    save_code_for_seed(config)

    routine_config = config.routine.copy()
    model_routine_config = model_config.routine.copy()
    train(
        agent, 
        model, 
        runner, 
        routine_config,
        model_routine_config
    )

    do_logging('Training completed')
