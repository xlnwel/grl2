import os
import collections
from functools import partial
import numpy as np
import ray

from core.elements.builder import ElementsBuilder
from core.log import do_logging
from core.utils import configure_gpu, set_seed, save_code_for_seed
from core.typing import get_basic_model_name
from tools.display import print_dict
from tools.plot import prepare_data_for_plotting, lineplot_dataframe
from tools.store import StateStore, TempStore
from tools.utils import modify_config, prefix_name
from tools.timer import Every, Timer
from .run import *
from algo.ppo.train import state_constructor, get_states, set_states, \
    build_agents, lookahead_optimize, ego_optimize


def model_train(model, model_buffer):
    if model_buffer.ready_to_sample():
        with Timer('model_train'):
            model.train_record()


def lookahead_run(agents, model, buffers, model_buffer, routine_config):
    def get_agent_states():
        state = [a.get_states() for a in agents]
        return state
    
    def set_agent_states(states):
        for a, s in zip(agents, states):
            a.set_states(s)

    # train lookahead agents
    with Timer('lookahead_run'):
        with TempStore(get_agent_states, set_agent_states):
            return run_on_model(
                model, model_buffer, agents, buffers, routine_config)


def lookahead_train(agents, model, buffers, model_buffer, routine_config, 
        aids, n_runs, run_fn, opt_fn):
    if not model_buffer.ready_to_sample():
        return
    assert n_runs >= 0, n_runs
    for _ in range(n_runs):
        out = run_fn(agents, model, buffers, model_buffer, routine_config)
        if out is not None:
            opt_fn(agents, routine_config, aids)


def ego_run(agents, runner, buffers, model_buffer, routine_config):
    all_aids = list(range(len(agents)))
    constructor = partial(state_constructor, agents=agents, runner=runner)
    get_fn = partial(get_states, agents=agents, runner=runner)
    set_fn = partial(set_states, agents=agents, runner=runner)

    for i, buffer in enumerate(buffers):
        assert buffer.size() == 0, f"buffer {i}: {buffer.size()}"

    with Timer('run'):
        with StateStore('real', constructor, get_fn, set_fn):
            runner.run(
                routine_config.n_steps, 
                agents, buffers, 
                model_buffer if routine_config.n_lookahead_steps > 0 else None, 
                all_aids, all_aids, 
                compute_return=routine_config.compute_return_at_once
            )

    for i, buffer in enumerate(buffers):
        assert buffer.ready(), f"buffer {i}: ({buffer.size()}, {len(buffer._queue)})"

    env_steps_per_run = runner.get_steps_per_run(routine_config.n_steps)
    for agent in agents:
        agent.add_env_step(env_steps_per_run)

    return agents[0].get_env_step()


def ego_train(agents, runner, buffers, model_buffer, routine_config, 
        aids, run_fn, opt_fn):
    env_step = run_fn(
        agents, runner, buffers, model_buffer, routine_config)
    train_step = opt_fn(agents, routine_config, aids)

    return env_step, train_step


def evaluate(agents, model, runner, env_step, routine_config):
    if routine_config.EVAL_PERIOD:
        get_fn = partial(get_states, agents=agents, runner=runner)
        set_fn = partial(set_states, agents=agents, runner=runner)
        def constructor():
            env_config = runner.env_config()
            if routine_config.n_eval_envs:
                env_config.n_envs = routine_config.n_eval_envs
            agent_states = [a.build_memory() for a in agents]
            runner_states = runner.build_env()
            return agent_states, runner_states

        with Timer('eval'):
            with StateStore('eval', constructor, get_fn, set_fn):
                eval_scores, eval_epslens, _, video = runner.eval_with_video(
                    agents, record_video=routine_config.RECORD_VIDEO
                )
        agents[0].store(**{
            'eval_score': np.mean(eval_scores), 
            'eval_epslen': np.mean(eval_epslens), 
        })
        if model is not None:
            model.store(**{
                'model_eval_score': eval_scores, 
                'model_eval_epslen': eval_epslens, 
            })
        if video is not None:
            agents[0].video_summary(video, step=env_step, fps=1)


def save(agents, model):
    with Timer('save'):
        for agent in agents:
            agent.save()
        model.save()


def modelpath2outdir(model_path):
    root_dir, model_name = model_path
    model_name = get_basic_model_name(model_name)
    outdir = '/'.join([root_dir, model_name])
    return outdir


def log_model_errors(errors, outdir, env_step):
    if errors:
        with Timer('error_log'):
            data = collections.defaultdict(dict)
            for k1, errs in errors.items():
                for k2, v in errs.items():
                    data[k2][k1] = v
            y = 'abs error'
            outdir = '/'.join([outdir, 'errors'])
            if not os.path.isdir(outdir):
                os.makedirs(outdir, exist_ok=True)
            for k, v in data.items():
                filename = f'{k}-{env_step}'
                filepath = '/'.join([outdir, filename])
                data[k] = prepare_data_for_plotting(
                    v, y=y, smooth_radius=1, filepath=filepath)
                lineplot_dataframe(data[k], filename, y=y, outdir=outdir)


def log(agents, model, env_step, train_step, errors):
    agent = agents[0]
    with Timer('log'):
        run_time = Timer('run').last()
        train_time = Timer('train').last()
        if run_time == 0:
            fps = 0
        else:
            fps = agent.get_env_step_intervals() / run_time
        if train_time == 0:
            tps = 0
        else:
            tps = agent.get_train_step_intervals() / train_time
        error_stats = {}
        for k1, errs in errors.items():
            for k2, v in errs.items():
                error_stats[f'{k1}-{k2}'] = v
        TRAIN = 'train'
        for k1, errs in errors.items():
            for k2 in errs.keys():
                if k1 != TRAIN:
                    k1_err = np.mean(error_stats[f'{k1}-{k2}'])
                    train_err = np.mean(error_stats[f'{TRAIN}-{k2}'])
                    k1_train_err = np.abs(k1_err - train_err)
                    error_stats[f'{k1}&{TRAIN}-{k2}'] = k1_train_err
                    error_stats[f'norm_{k1}&{TRAIN}-{k2}'] = \
                        k1_train_err / train_err if train_err else k1_train_err
        error_stats = prefix_name(error_stats, 'model_error')
        agent.store(**{
                'stats/train_step': train_step, 
                'time/fps': fps, 
                'time/tps': tps, 
            }, 
            **error_stats, 
            **Timer.all_stats()
        )
        score = agent.get_raw_item('score')
        agent.store(score=score)
        agent.record(step=env_step)
        for agent in agents:
            agent.clear()

        if model is not None:
            train_step = model.get_train_step()
            model_train_duration = Timer('model_train').last()
            if model_train_duration == 0:
                tps = 0
            else:
                tps = model.get_train_step_intervals() / model_train_duration
            model.store(**{
                    'stats/train_step': train_step, 
                    'time/tps': tps, 
                }, 
                **Timer.all_stats()
            )
            model.store(model_score=score)
            model.record(step=env_step)


def training_aids(all_aids, routine_config):
    aids = np.random.choice(
        all_aids, size=len(all_aids), replace=False, 
        p=routine_config.perm)
    return aids


def train(
    agents, 
    model, 
    runner, 
    buffers, 
    model_buffer, 
    routine_config, 
    model_routine_config, 
    aids_fn=training_aids,
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
    env_step = agents[0].get_env_step()
    to_record = Every(
        routine_config.LOG_PERIOD, 
        start=env_step, 
        init_next=env_step != 0, 
        final=routine_config.MAX_STEPS
    )
    all_aids = list(range(len(agents)))

    while env_step < routine_config.MAX_STEPS:
        errors = AttrDict()
        aids = aids_fn(all_aids, routine_config)
        time2record = agents[0].contains_stats('score') \
            and to_record(env_step)
        
        model_train_fn(
            model, 
            model_buffer
        )
        if routine_config.quantify_model_errors and time2record:
            errors.train = quantify_model_errors(
                agents, model, runner.env_config(), MODEL_EVAL_STEPS, [])

        # if model is None or (model_routine_config.model_warm_up and env_step < model_routine_config.model_warm_up_steps):
        #     pass
        # else:
        lka_train_fn(
            agents, 
            model, 
            buffers, 
            model_buffer, 
            routine_config, 
            aids=aids, 
            n_runs=routine_config.n_lookahead_steps, 
            run_fn=lka_run_fn, 
            opt_fn=lka_opt_fn
        )
        if routine_config.quantify_model_errors and time2record:
            errors.lka = quantify_model_errors(
                agents, model, runner.env_config(), MODEL_EVAL_STEPS, None)

        env_step, train_step = ego_train_fn(
            agents, 
            runner, 
            buffers, 
            model_buffer, 
            routine_config, 
            aids=aids, 
            run_fn=ego_run_fn, 
            opt_fn=ego_opt_fn
        )
        if routine_config.quantify_model_errors and time2record:
            errors.ego = quantify_model_errors(
                agents, model, runner.env_config(), MODEL_EVAL_STEPS, [])

        if time2record:
            evaluate(agents, model, runner, env_step, routine_config)
            if routine_config.quantify_model_errors:
                outdir = modelpath2outdir(agents[0].get_model_path())
                log_model_errors(errors, outdir, env_step)
            save(agents, model)
            log(agents, model, env_step, train_step, errors)


def build_model(config, model_config, env_stats):
    root_dir = config.root_dir
    model_name = get_basic_model_name(config.model_name)
    seed = config.seed
    new_model_name = '/'.join([model_name, 'dynamics'])
    model_config = modify_config(
        model_config, 
        max_layer=1, 
        aid=0,
        algorithm=config.dynamics_name, 
        name=config.algorithm, 
        info=config.info,
        model_info=config.model_info,
        n_runners=config.env.n_runners, 
        n_envs=config.env.n_envs, 
        root_dir=root_dir, 
        model_name=new_model_name, 
        overwrite_existed_only=True, 
        seed=seed+1000
    )

    builder = ElementsBuilder(
        model_config, 
        env_stats, 
        to_save_code=False, 
        max_steps=config.routine.MAX_STEPS
    )
    elements = builder.build_agent_from_scratch(config=model_config)
    model = elements.agent
    model_buffer = elements.buffer

    return model, model_buffer


def main(configs, train=train):
    assert len(configs) > 1, len(configs)
    config, model_config = configs[0], configs[-1]
    if config.routine.compute_return_at_once:
        config.buffer.sample_keys += ['advantage', 'v_target']
    seed = config.get('seed')
    set_seed(seed)

    configure_gpu()
    use_ray = config.env.get('n_runners', 1) > 1
    if use_ray:
        from tools.ray_setup import sigint_shutdown_ray
        ray.init(num_cpus=config.env.n_runners)
        sigint_shutdown_ray()

    runner = Runner(config.env)

    env_stats = runner.env_stats()
    # assert len(configs) == env_stats.n_agents, (len(configs), env_stats.n_agents)
    env_stats.n_envs = config.env.n_runners * config.env.n_envs
    print_dict(env_stats)

    # build agents
    agents, buffers = build_agents(config, env_stats)
    # build model
    model, model_buffer = build_model(config, model_config, env_stats)
    save_code_for_seed(config)

    routine_config = config.routine.copy()
    model_routine_config = model_config.routine.copy()
    train(
        agents, 
        model, 
        runner, 
        buffers, 
        model_buffer, 
        routine_config, 
        model_routine_config
    )

    do_logging('Training completed')
