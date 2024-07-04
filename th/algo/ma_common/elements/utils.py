import numpy as np
import torch

from core.names import DEFAULT_ACTION, PATH_SPLIT
from core.typing import AttrDict
from tools.utils import expand_shape_match
from tools.tree_ops import tree_map
from th.tools import th_loss, th_math, th_utils

UNIT_DIM = 2

def get_initial_state(state, i):
  return tree_map(lambda x: x[:, i], state)


def reshape_for_bptt(*args, bptt):
  return tree_map(
    lambda x: x.reshape(-1, bptt, *x.shape[2:]), args
  )


def compute_values(func, data, state, bptt, seq_axis=1):
  if state is None:
    curr_data = AttrDict(
      global_state=data.global_state, prev_info=data.prev_info
    )
    next_data = AttrDict(
      global_state=data.next_global_state, prev_info=data.next_prev_info
    )
    value = func(curr_data, return_state=False)
    next_value = func(next_data, return_state=False)
  else:
    state_reset, next_state_reset = th_utils.split_data(data.state_reset, axis=seq_axis)
    curr_data = AttrDict(
      global_state=data.global_state, prev_info=data.prev_info, state_reset=state_reset
    )
    next_data = AttrDict(
      global_state=data.next_global_state, prev_info=data.next_prev_info, state_reset=next_state_reset
    )
    shape = data.global_state.shape[:-1]
    assert isinstance(bptt, int), bptt
    curr_data, next_data, state = reshape_for_bptt(
      curr_data, next_data, state, bptt=bptt
    )
    curr_state = get_initial_state(state, 0)
    next_state = get_initial_state(state, 1)
    value = func(curr_data, curr_state, return_state=False)
    next_value = func(next_data, next_state, return_state=False)
    value, next_value = tree_map(
      lambda x: x.reshape(shape), (value, next_value)
    )

  next_value = next_value.detach()

  return value, next_value


def compute_policy_dist(model, data, state, bptt):
  data = AttrDict(
    obs=data.obs, state_reset=data.state_reset, 
    action_mask=data.action_mask, prev_info=data.prev_info
  )
  shape = data.obs.shape[:-1]
  if state is None:
    act_outs = model.forward_policy(data, return_state=False)
  else:
    data, state = reshape_for_bptt(data, state, bptt=bptt)
    state = get_initial_state(state, 0)
    act_outs = model.forward_policy(data, state, return_state=False)
    act_outs = tree_map(
      lambda x: x.reshape(*shape, -1) if x.ndim > len(shape) else x, act_outs
    )
  act_dists = model.policy_dist(act_outs)
  return act_dists


def compute_policy(model, data, state, bptt):
  act_dists = compute_policy_dist(model, data, state, bptt)
  pi_logprob = sum([ad.log_prob(data.action[k]) for k, ad in act_dists.items()])
  log_ratio = pi_logprob - data.mu_logprob
  ratio = torch.exp(log_ratio)
  return act_dists, pi_logprob, log_ratio, ratio


def prefix_name(terms, name):
  if name is not None:
    new_terms = AttrDict()
    for k, v in terms.items():
      if PATH_SPLIT not in k:
        new_terms[f'{name}{PATH_SPLIT}{k}'] = v
      else:
        new_terms[k] = v
    return new_terms
  return terms


def compute_gae(reward, discount, value, gamma, gae_discount, 
                next_value=None, reset=None):
  if next_value is None:
    value, next_value = value[:, :-1], value[:, 1:]
  elif next_value.ndim < value.ndim:
    next_value = np.expand_dims(next_value, 1)
    next_value = np.concatenate([value[:, 1:], next_value], 1)
  assert reward.shape == discount.shape == value.shape == next_value.shape, (reward.shape, discount.shape, value.shape, next_value.shape)
  
  delta = (reward + discount * gamma * next_value - value).astype(np.float32)
  discount = (discount if reset is None else (1 - reset)) * gae_discount
  
  next_adv = 0
  advs = np.zeros_like(reward, dtype=np.float32)
  for i in reversed(range(advs.shape[1])):
    advs[:, i] = next_adv = (delta[:, i] + discount[:, i] * next_adv)
  traj_ret = advs + value

  return advs, traj_ret


def compute_actor_loss(config, data, stats, act_dists, entropy_coef):
  if config.get('policy_sample_mask', True):
    sample_mask = data.sample_mask
  else:
    sample_mask = None
  if stats.advantage.ndim < stats.ratio.ndim:
    stats.advantage = expand_shape_match(stats.advantage, stats.ratio, np=torch)

  if config.pg_type == 'pg':
    raw_pg_loss = th_loss.pg_loss(
      advantage=stats.advantage, 
      logprob=stats.pi_logprob, 
    )
  elif config.pg_type == 'is':
    raw_pg_loss = th_loss.is_pg_loss(
      advantage=stats.advantage, 
      ratio=stats.ratio, 
    )
  elif config.pg_type == 'ppo':
    ppo_pg_loss, ppo_clip_loss, raw_pg_loss = \
      th_loss.ppo_loss(
        advantage=stats.advantage, 
        ratio=stats.ratio, 
        clip_range=config.ppo_clip_range, 
      )
    stats.ppo_pg_loss = ppo_pg_loss
    stats.ppo_clip_loss = ppo_clip_loss
  elif config.pg_type == 'correct_ppo':
    cppo_pg_loss, cppo_clip_loss, raw_pg_loss = \
      th_loss.correct_ppo_loss(
        advantage=stats.advantage, 
        pi_logprob=stats.pi_logprob, 
        mu_logprob=data.mu_logprob, 
        clip_range=config.ppo_clip_range, 
        opt_pg=config.opt_pg
      )
    stats.cppo_pg_loss = cppo_pg_loss
    stats.cppo_clip_loss = cppo_clip_loss
  else:
    raise NotImplementedError
  if raw_pg_loss.ndim == 4:   # reduce the action dimension for continuous actions
    raw_pg_loss = raw_pg_loss.sum(-1)
  scaled_pg_loss, pg_loss = th_loss.to_loss(
    raw_pg_loss, stats.pg_coef, mask=sample_mask, replace=None
  )
  stats.raw_pg_loss = raw_pg_loss
  stats.scaled_pg_loss = scaled_pg_loss
  stats.pg_loss = pg_loss

  entropy = {k: ad.entropy() for k, ad in act_dists.items()}
  for k, v in entropy.items():
    stats[f'{k}_entropy'] = v
  entropy = sum(entropy.values())
  scaled_entropy_loss, entropy_loss = th_loss.entropy_loss(
    entropy_coef=entropy_coef, 
    entropy=entropy, 
    mask=sample_mask, 
    replace=None
  )
  stats.entropy = entropy
  stats.scaled_entropy_loss = scaled_entropy_loss
  stats.entropy_loss = entropy_loss

  loss = pg_loss + entropy_loss
  stats.actor_loss = loss

  if sample_mask is not None:
    sample_mask = expand_shape_match(sample_mask, stats.ratio, np=torch)
  clip_frac = th_math.mask_mean(
    (torch.abs(stats.ratio - 1.) > config.get('ppo_clip_range', .2)).float(), 
    sample_mask, replace=None
  )
  stats.clip_frac = clip_frac
  stats.approx_kl = th_math.mask_mean(
    .5 * (data.mu_logprob - stats.pi_logprob)**2, sample_mask, replace=None
  )

  return loss, stats


def compute_vf_loss(config, data, stats):
  if config.get('value_sample_mask', False):
    sample_mask = data.sample_mask
  else:
    sample_mask = None
  
  v_target = stats.v_target

  value_loss_type = config.value_loss
  if value_loss_type == 'huber':
    raw_value_loss = th_loss.huber_loss(
      stats.value, 
      y=v_target, 
      threshold=config.huber_threshold
    )
  elif value_loss_type == 'mse':
    raw_value_loss = .5 * (stats.value - v_target)**2
  elif value_loss_type == 'clip' or value_loss_type == 'clip_huber':
    old_value, _ = th_utils.split_data(
      data.value, data.next_value, axis=1
    )
    raw_value_loss, stats.v_clip_frac = th_loss.clipped_value_loss(
      stats.value, 
      v_target, 
      old_value, 
      config.value_clip_range, 
      huber_threshold=config.huber_threshold, 
      mask=sample_mask, 
    )
  else:
    raise ValueError(f'Unknown value loss type: {value_loss_type}')
  stats.raw_value_loss = raw_value_loss
  scaled_value_loss, value_loss = th_loss.to_loss(
    raw_value_loss, 
    coef=stats.value_coef, 
    mask=sample_mask, 
    replace=None
  )
  
  stats.scaled_value_loss = scaled_value_loss
  stats.value_loss = value_loss

  return value_loss, stats


def record_target_adv(stats):
  stats.explained_variance = th_math.explained_variance(
    stats.v_target, stats.value)
  # stats.v_target_unit_std = jnp.std(stats.v_target, axis=-1)
  # stats.raw_adv_unit_std = jnp.std(stats.raw_adv, axis=-1)
  return stats


def record_policy_stats(data, stats, act_dists):
  if len(act_dists) == 1:
    stats.update(act_dists[DEFAULT_ACTION].get_stats(prefix='pi'))
  else:
    for k, ad in act_dists.items():
      k.replace('action_', '')
      stats.update(ad.get_stats(prefix=f'{k}_pi'))

  return stats


def summarize_adv_ratio(stats, data):
  # if stats.raw_adv.ndim < stats.ratio.ndim:
  #   raw_adv = expand_dims_match(stats.raw_adv, stats.ratio)
  # else:
  #   raw_adv = stats.raw_adv
  # stats.raw_adv_ratio_pp = jnp.logical_and(raw_adv > 0, stats.ratio > 1)
  # stats.raw_adv_ratio_pn = jnp.logical_and(raw_adv > 0, stats.ratio < 1)
  # stats.raw_adv_ratio_np = jnp.logical_and(raw_adv < 0, stats.ratio > 1)
  # stats.raw_adv_ratio_nn = jnp.logical_and(raw_adv < 0, stats.ratio < 1)
  # stats.raw_adv_zero = raw_adv == 0
  # stats.ratio_one = stats.ratio == 1
  # stats.adv_ratio_pp = jnp.logical_and(stats.advantage > 0, stats.ratio > 1)
  # stats.adv_ratio_pn = jnp.logical_and(stats.advantage > 0, stats.ratio < 1)
  # stats.adv_ratio_np = jnp.logical_and(stats.advantage < 0, stats.ratio > 1)
  # stats.adv_ratio_nn = jnp.logical_and(stats.advantage < 0, stats.ratio < 1)
  # stats.adv_zero = stats.advantage == 0
  # stats.pn_ratio = jnp.where(stats.adv_ratio_pn, stats.ratio, 0)
  # stats.np_ratio = jnp.where(stats.adv_ratio_np, stats.ratio, 0)

  return stats
