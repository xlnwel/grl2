from tools.timer import timeit
from tools.utils import yield_from_tree
from algo.lka_common.run import *


def add_data_to_buffer(agent, data):
    for step_data in data:
        for d in yield_from_tree(step_data):
            agent.buffer.merge(d) 


@timeit
def branched_rollout(agent, dynamics, routine_config, rng, lka_aids):
    env_output, states = initialize_for_dynamics_run(agent, dynamics, routine_config)
    if env_output is None:
        return

    if not routine_config.switch_model_at_every_step:
        dynamics.model.choose_elite()
    agent.model.switch_params(True, lka_aids)
    agent_params, dynamics_params = prepare_params(agent, dynamics)
    agent_obs_rms, agent_obs_clip, dynamics_obs_rms, dynamics_dim_mask = \
        prepare_rms(agent, dynamics)

    data, env_output, _ = rollout(
        agent.model, agent_params, 
        dynamics.model, dynamics_params, 
        rng, env_output, states, 
        routine_config.n_simulated_steps, 
        agent_obs_rms=agent_obs_rms, 
        agent_obs_clip=agent_obs_clip, 
        dynamics_obs_rms=dynamics_obs_rms, 
        dynamics_dim_mask=dynamics_dim_mask, 
    )
    add_data_to_buffer(agent, data)

    agent.model.switch_params(False, lka_aids)
    agent.model.check_params(False)
