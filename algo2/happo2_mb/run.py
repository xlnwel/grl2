import numpy as np
from functools import partial
import jax
import jax.numpy as jnp

from core.typing import AttrDict, dict2AttrDict
from tools.display import print_dict_info
from tools.utils import batch_dicts
from tools.timer import timeit
from env.typing import EnvOutput
from env.func import create_env
from algo.ppo.run import prepare_buffer, concat_along_unit_dim, compute_gae, \
    Runner as RunnerBase


class Runner(RunnerBase):
    def run(
        self, 
        n_steps, 
        agents, 
        model_buffer, 
        lka_aids, 
        collect_ids, 
        store_info=True,
        compute_return=True, 
    ):
        for aid, agent in enumerate(agents):
            if aid in lka_aids:
                agent.model.switch_params(True)
            else:
                agent.model.check_params(False)

        env_output = self.env_output
        env_outputs = [EnvOutput(*o) for o in zip(*env_output)]
        for _ in range(n_steps):
            acts, stats = zip(*[a(eo) for a, eo in zip(agents, env_outputs)])

            action = concat_along_unit_dim(acts)
            new_env_output = self.env.step(action)
            new_env_outputs = [EnvOutput(*o) for o in zip(*new_env_output)]

            next_obs = self.env.prev_obs()
            for i in collect_ids:
                data = dict(
                    obs=env_outputs[i].obs, 
                    action=acts[i], 
                    reward=new_env_outputs[i].reward, 
                    discount=new_env_outputs[i].discount, 
                    next_obs=next_obs[i], 
                    reset=new_env_outputs[i].reset, 
                    **stats[i]
                )
                agents[i].buffer.collect(**data)

            state = [s['state'] for s in stats] if 'state' in stats[0] else None
            if state is not None:
                state = batch_dicts(state, func=lambda x: np.stack(x, 1))

            data = dict(
                reset=concat_along_unit_dim(new_env_output.reset),
                obs=batch_dicts(env_output.obs, func=concat_along_unit_dim),
                action=action, 
                reward=concat_along_unit_dim(new_env_output.reward),
                discount=concat_along_unit_dim(new_env_output.discount),
                next_obs=batch_dicts(next_obs, func=concat_along_unit_dim), 
            )
            if state is not None:
                data['state'] = state
            if model_buffer is not None:
                model_buffer.collect(**data)

            if store_info:
                done_env_ids = [i for i, r in enumerate(new_env_outputs[0].reset) if np.all(r)]

                if done_env_ids:
                    info = self.env.info(done_env_ids)
                    if info:
                        info = batch_dicts(info, list)
                        for agent in agents:
                            agent.store(**info)
            env_output = new_env_output
            env_outputs = new_env_outputs

        prepare_buffer(collect_ids, agents, env_outputs, compute_return)

        for i in lka_aids:
            agents[i].model.switch_params(False)
        for agent in agents:
            agent.model.check_params(False)

        self.env_output = env_output
        return env_outputs


def split_env_output(env_output, aid2uids):
    env_outputs = [
        jax.tree_util.tree_map(lambda x: x[:, uids], env_output) 
        for uids in aid2uids
    ]
    return env_outputs


@partial(jax.jit, static_argnums=[0, 2, 6])
def rollout(agents, agents_params, env, env_params, rng, env_output, n_steps):
    data_list = [[] for a in agents]
    agent_inps = [env_output.obs.slice((slice(None), uids)) 
        for uids in env.env_stats.aid2uids]

    for _ in jnp.arange(n_steps):
        rng, agent_rng, env_rng = jax.random.split(rng, 3)

        actions, stats = [], []
        agent_rngs = jax.random.split(agent_rng, len(agents))
        for agent, ap, arng, inp in zip(agents, agents_params, agent_rngs, agent_inps):
            action, stat, _ = agent.raw_action(ap, arng, inp)
            actions.append(action)
            stats.append(stat)

        env_inp = env_output.obs.copy()
        env_inp.action = concat_along_unit_dim(actions)
        env_inp.obs_loc = env_params.obs_loc
        env_inp.obs_scale = env_params.obs_scale
        # TODO: switch model at every step
        new_env_output, _, _ = env.raw_action(env_params, env_rng, env_inp)

        next_obs = [new_env_output.obs.slice((slice(None), uids)) 
            for uids in env.env_stats.aid2uids]
        for i, inp in enumerate(agent_inps):
            data = inp
            uids = env.env_stats.aid2uids[i]
            data.update(dict(
                action=actions[i], 
                reward=new_env_output.reward[:, uids], 
                discount=new_env_output.discount[:, uids], 
                reset=new_env_output.reset[:, uids], 
                **stats[i]
            ))
            data.update({f'next_{k}': v for k, v in next_obs[i].items()})
            data_list[i].append(data)
        env_output = new_env_output
        agent_inps = next_obs

    return data_list, env_output


def add_data_to_buffer(
    agent, 
    data, 
    env_output, 
    compute_return=True, 
):
    value = agent.compute_value(env_output)
    buffer = agent.buffer
    data = batch_dicts(data, lambda x: np.stack(x, 1))
    data.value = np.concatenate([data.value, np.expand_dims(value, 1)], 1)

    if compute_return:
        value = data.value[:, :-1]
        if agent.trainer.config.popart:
            data.value = agent.trainer.popart.denormalize(data.value)
        data.value, data.next_value = data.value[:, :-1], data.value[:, 1:]
        data.advantage, data.v_target = compute_gae(
            reward=data.reward, 
            discount=data.discount,
            value=data.value,
            gamma=buffer.config.gamma,
            gae_discount=buffer.config.gamma * buffer.config.lam,
            next_value=data.next_value, 
            reset=data.reset,
        )
        if agent.trainer.config.popart:
            # reassign value to ensure value clipping at the right anchor
            data.value = value
    buffer.move_to_queue(data)


@timeit
def simultaneous_rollout(env, agents, env_output, routine_config, rng):
    for agent in agents:
        agent.model.switch_params(True)
        agent.set_states()
    
    if not routine_config.switch_model_at_every_step:
        env.model.choose_elite()
    env_params = env.model.params
    env_params.obs_loc, env_params.obs_scale = env.model.obs_rms.get_rms_stats(False)
    agents_model = tuple([agent.model for agent in agents])
    agents_params = [agent.model.params for agent in agents]

    data_list, env_output = rollout(
        env.model, env_params, 
        agents_model, agents_params, 
        rng, env_output, 
        routine_config.n_simulated_steps, 
    )

    env_outputs = split_env_output(env_output, env.env_stats.aid2uids)
    for agent, data, eo in zip(agents, data_list, env_outputs):
        add_data_to_buffer(agent, data, eo, 
            routine_config.compute_return_at_once)

    for agent in agents:
        agent.model.switch_params(False)

    return env_output


@timeit
def unilateral_rollout(env, agents, env_output, routine_config, rng):
    for i, agent in enumerate(agents):
        for a in agents:
            a.set_states()
        agent.model.switch_params(True)
        env.model.choose_elites()

        env_params = env.model.params
        env_params.obs_loc, env_params.obs_scale = env.model.obs_rms.get_rms_stats(False)
        agents_model = [a.model for a in agents]
        agents_params = [a.model.params for a in agents]

        data_list, env_output = rollout(
            env.model, env_params, 
            agents_model, agents_params, 
            rng, env_output, 
            routine_config.n_simulated_steps, 
        )

        env_outputs = split_env_output(env_output, env.env_stats.aid2uids)
        add_data_to_buffer(agent, data_list[i], env_outputs[i], 
            routine_config.compute_return_at_once)

        agent.model.switch_params(False)

    return env_outputs


@timeit
def initialize_for_dynamics_run(agents, dynamics, routine_config):
    sample_keys = agents[0].buffer.obs_keys + ['state'] \
        if routine_config.restore_state else agent.buffer.obs_keys
    obs = dynamics.buffer.sample_from_recency(
        batch_size=routine_config.n_simulated_envs, 
        sample_keys=sample_keys, 
    )
    if obs is None:
        return
    basic_shape = obs.obs.shape[:-1]
    reward = np.zeros(basic_shape, np.float32)
    discount = np.ones(basic_shape, np.float32)
    reset = np.zeros(basic_shape, np.float32)

    env_output = EnvOutput(obs, reward, discount, reset)

    if routine_config.restore_state:
        states = obs.pop('state')
        states = [states.slice((slice(None), 0)), states.slice((slice(None), 1))]
        for a, s in zip(agents, states):
            a.set_states(s)
    else:
        for a in agents:
            a.set_states()
    
    return env_output


@timeit
def prepare_params(agents, dynamisc):
    agents_params = [agent.model.params for agent in agents]
    dynamics_params = dynamisc.model.params
    dynamics_params.obs_loc, dynamics_params.obs_scale = \
        dynamisc.model.obs_rms.get_rms_stats(False)

    return agents_params, dynamics_params


@timeit
def branched_rollout(agents, agents_params, dynamics, dynamics_params, routine_config, rng, lka_aids):
    env_output = initialize_for_dynamics_run(agents, dynamics, routine_config)
    if env_output is None:
        return

    agents_model = tuple([a.model for a in agents])
    [agents_model[i].switch_params(True) for i in lka_aids]
    
    data_list, env_output = rollout(
        agents_model, agents_params, 
        dynamics.model, dynamics_params, 
        rng, env_output, routine_config.n_simulated_steps
    )

    env_outputs = split_env_output(env_output, dynamics.env_stats.aid2uids)
    for i in lka_aids:
        add_data_to_buffer(agents[i], data_list[i], env_outputs[i], 
            routine_config.compute_return_at_once)

    [agents_model[i].switch_params(False) for i in lka_aids]
    [a.model.check_params() for a in agents]


def concat_env_output(env_output):
    obs = batch_dicts(env_output.obs, concat_along_unit_dim)
    reward = concat_along_unit_dim(env_output.reward)
    discount = concat_along_unit_dim(env_output.discount)
    reset = concat_along_unit_dim(env_output.reset)
    return EnvOutput(obs, reward, discount, reset)


@timeit
def quantify_dynamics_errors(agents, model, env_config, n_steps, lka_aids):
    model.model.choose_elite(0)
    if lka_aids is None:
        lka_aids = list(range(len(agents)))
    for aid in lka_aids:
        agents[aid].model.check_params(False)
        agents[aid].model.switch_params(True)

    errors = AttrDict()
    errors.trans = []
    errors.reward = []
    errors.discount = []

    env = create_env(env_config)
    env_output = env.output()
    env_outputs = [EnvOutput(*o) for o in zip(*env_output)]
    env_output = concat_env_output(env_output)
    for _ in range(n_steps):
        acts, _ = zip(*[a(eo) for a, eo in zip(agents, env_outputs)])
        action = concat_along_unit_dim(acts)

        new_env_output = env.step(action)
        new_env_outputs = [EnvOutput(*o) for o in zip(*new_env_output)]
        new_env_output = concat_env_output(new_env_output)
        env_output.obs['action'] = action
        new_model_output, _ = model(env_output)
        errors.trans.append(
            np.abs(new_env_output.obs['obs'] - new_model_output.obs['obs']).reshape(-1))
        errors.reward.append(
            np.abs(new_env_output.reward - new_model_output.reward).reshape(-1))
        errors.discount.append(
            np.abs(new_env_output.discount - new_model_output.discount).reshape(-1))
        env_output = new_env_output
        env_outputs = new_env_outputs

    for k, v in errors.items():
        errors[k] = np.stack(v, -1)
    for aid in lka_aids:
        agents[aid].model.switch_params(False)

    return errors
