import os
from datetime import datetime
import collections
import time
from typing import Any, Dict
import cloudpickle
import numpy as np
import ray

from tools import pickle
from core.elements.monitor import Monitor as ModelMonitor
from core.names import TRAIN_STEP
from core.typing import ModelPath, ModelStats
from rule.utils import is_rule_strategy
from tools.log import do_logging
from tools.timer import Timer
from tools.utils import dict2AttrDict, config_attr
from distributed.common.names import *
from distributed.common.remote.base import RayBase
from distributed.common.typing import LoggedStats, LoggedType
from distributed.common.utils import matrix_tb_plot
from .parameter_server import ParameterServer

def _fill_nan(stats):
  stats[np.isnan(stats)] = np.nanmin(stats)
  return stats


class Monitor(RayBase):
  def __init__(
    self, 
    config: dict,
    parameter_server: ParameterServer, 
    name='monitor'
  ):
    super().__init__(seed=config.get('seed'))
    self.config = dict2AttrDict(config['monitor'])
    self.print_terminal_info = self.config.get('print_terminal_info', True)
    self.n_agents = config['n_agents']
    self.self_play = config.get('self_play', False)
    self.parameter_server = parameter_server

    self.monitor = None
    self.monitors: Dict[ModelPath, ModelMonitor] = {}
    self._recording_stats: Dict[Dict[ModelPath, Any]] = collections.defaultdict(dict)

    self._train_steps: Dict[ModelPath, int] = collections.defaultdict(lambda: 0)
    self._train_steps_in_period: Dict[ModelPath, int] = collections.defaultdict(lambda: 0)
    self._env_steps: Dict[ModelPath, int] = collections.defaultdict(lambda: 0)
    self._env_steps_in_period: Dict[ModelPath, int] = collections.defaultdict(lambda: 0)
    self._episodes: Dict[ModelPath, int] = collections.defaultdict(lambda: 0)
    self._episodes_in_period: Dict[ModelPath, int] = collections.defaultdict(lambda: 0)

    self._last_save_time: Dict[ModelPath, float] = collections.defaultdict(lambda: time.time())

    self.dir = os.path.join(self.config.root_dir, self.config.model_name)
    self.name = name
    self.path = os.path.join(self.dir, 'monitor.pkl')

    self.restore()

  def set_max_steps(self, max_steps):
    self._max_steps = max_steps

  def build_monitor_for_model(self, model_path: ModelPath):
    if model_path not in self.monitors:
      self.monitors[model_path] = ModelMonitor(
        model_path, name=model_path.model_name, max_steps=self._max_steps)
      self._last_save_time[model_path] = time.time()

  """ Stats Management """
  def store_stats_for_model(
    self, 
    model_path: ModelPath, 
    stats: Dict, 
    step: int=None, 
    record=False, 
  ):
    self.build_monitor_for_model(model_path)
    self.monitors[model_path].store(**stats)
    if record:
      self.record_for_model(model_path, step)

  def store_stats(
    self, 
    *, 
    stats: Dict, 
    step: int, 
    record=False
  ):
    if self.monitor is None:
      self.monitor = ModelMonitor(
        ModelPath(self.config.root_dir, self.config.model_name), 
        name=self.config.model_name, max_steps=self._max_steps
      )
    self.monitor.store(**stats)
    self.monitor.set_step(step)
    if record:
      self.monitor.record(step, print_terminal_info=self.print_terminal_info)

  def store_train_stats(self, model_stats: ModelStats):
    model, stats = model_stats
    train_step = stats.pop(TRAIN_STEP)
    self._train_steps_in_period[model] = train_step - self._train_steps[model]
    self._train_steps[model] = train_step
    self.store_stats_for_model(model, stats)

  def store_run_stats(self, model_stats: ModelStats):
    model, stats = model_stats
    # assert stats['train_steps'] == self._train_steps[model], (stats['train_steps'], self._train_steps[model])
    stats.pop('train_steps', None)
    self.build_monitor_for_model(model)
    env_steps = stats.pop('env_steps')
    self._env_steps[model] += env_steps
    self._env_steps_in_period[model] += env_steps
    n_episodes = stats.pop('n_episodes')
    self._episodes[model] += n_episodes
    self._episodes_in_period[model] += n_episodes

    stats = {
      k if k.endswith('score') or '/' in k else f'run/{k}': v
      for k, v in stats.items()
    }
    self.store_stats_for_model(
      model, 
      stats, 
      record=is_rule_strategy(model), 
    )

  def retrieve_run_stats(self, model: ModelPath):
    duration = time.time() - self._last_save_time[model]
    assert duration > 0, duration
    stats = {
      'time/tps': self._train_steps_in_period[model] / duration,
      'time/fps': self._env_steps_in_period[model] / duration,
      'time/eps': self._episodes_in_period[model] / duration,
      # 'stats/env_step': self._env_steps[model],
      'stats/train_step': self._train_steps[model],
      'run/n_episodes': self._episodes[model],
    }
    self._train_steps_in_period[model] = 0
    self._env_steps_in_period[model] = 0
    self._episodes_in_period[model] = 0
    return stats

  def record_for_model(self, model: ModelPath, step: int):
    stats = self.retrieve_run_stats(model)
    self.monitors[model].store(**stats)
    self.monitors[model].set_step(step)
    self.monitors[model].record(print_terminal_info=self.print_terminal_info)
    self._last_save_time[model] = time.time()

  def clear_iteration_stats(self):
    self._recording_stats.clear()
    self.monitors.clear()
    self._last_save_time.clear()

  """ Checkpoints """
  def restore(self):
    data = pickle.restore(filedir=self.dir, filename=self.name, name=self.name)
    self.load(data)

  def retrieve(self):
    stats = {v: getattr(self, v) for v in vars(self) if v.startswith('_')}
    return stats

  def load(self, data: Dict):
    config_attr(self, data, filter_dict=False, config_as_attr=False, 
                private_attr=True, check_overwrite=True)

  def save(self):
    data = {v: getattr(self, v) for v in vars(self) if v.startswith('_')}
    pickle.save(data, filedir=self.dir, filename=self.name, name=self.name, atomic=True)

  def retrieve_all(self, step: int):
    all_stats = []
    oid = self.parameter_server.get_active_aux_stats.remote()
    if self.n_agents != 2:
      active_stats = ray.get(oid)
      for model, stats in active_stats.items():
        self.store_stats_for_model(model, stats, step=step, record=False)
        stats = self.retrieve_run_stats(model)
        stats.update(self.monitors[model].get_stats())
        all_stats.append(LoggedStats(model, LoggedType.MONITOR, stats))
    else:
      active_stats, dists = ray.get([
        oid, 
        self.parameter_server.get_opponent_distributions_for_active_models.remote()
      ])
      for model, (payoff, dist) in dists.items():
        self.store_stats_for_model(model, active_stats[model], step=step, record=False)
        stats = self.retrieve_run_stats(model)
        stats.update(self.monitors[model].get_stats())
        all_stats.append(LoggedStats(model, LoggedType.MONITOR, stats))
        all_stats.append(self.retrieve_plot_stats(model, 'payoff', payoff, fill_nan=True))
        all_stats.append(self.retrieve_plot_stats(model, 'opp_dist', dist))
    return all_stats
    
  def save_all(self, step: int):
    oid = self.parameter_server.get_active_aux_stats.remote()
    self.save()
    if self.n_agents != 2:
      active_stats = ray.get(oid)
      for model, stats in active_stats.items():
        self.store_stats_for_model(model, stats, step=step, record=True)
    else:
      active_stats, dists = ray.get([
        oid, 
        self.parameter_server.get_opponent_distributions_for_active_models.remote()
      ])
      for model, (payoff, dist) in dists.items():
        self.store_stats_for_model(model, active_stats[model], step=step, record=True)
        with Timer('Monitor Real-Time Plot Time', period=1):
          self.plot_recording_stats(model, 'payoff', payoff, fill_nan=True)
          self.plot_recording_stats(model, 'opp_dist', dist)

    with open('check.txt', 'w') as f:
      f.write(datetime.strftime(datetime.now(), '%Y-%m-%d %H:%M:%S'))

  def retrieve_payoff_table(self, step: int=None):
    stats = []
    if self.n_agents == 2:
      with Timer('Monitor Retrieval Time', period=1):
        models, payoffs, counts = ray.get([
          self.parameter_server.get_active_models.remote(), 
          self.parameter_server.get_payoffs.remote(fill_nan=True), 
          self.parameter_server.get_counts.remote()
        ])
      if self.self_play:
        kwargs = {
          'stats': payoffs, 
          'xlabel': 'Player2', 
          'ylabel': 'Player1', 
          'name': 'payoff', 
          'step': step
        }
        stats.append(LoggedStats(models[0], LoggedType.GRAPH, kwargs))
        kwargs = {
          'stats': counts, 
          'xlabel': 'Player2', 
          'ylabel': 'Player1', 
          'name': 'count', 
          'step': step
        }
        stats.append(LoggedStats(models[0], LoggedType.GRAPH, kwargs))
      else:
        for m, p, c in zip(models, payoffs, counts):
          kwargs = {
            'stats': p, 
            'xlabel': 'Player2', 
            'ylabel': 'Player1', 
            'name': 'payoff', 
            'step': step
          }
          stats.append(LoggedStats(m, LoggedType.GRAPH, kwargs))
          kwargs = {
            'stats': c, 
            'xlabel': 'Player2', 
            'ylabel': 'Player1', 
            'name': 'count', 
            'step': step
          }
          stats.append(LoggedStats(m, LoggedType.GRAPH, kwargs))
    return stats
      
  def save_payoff_table(self, step: int=None):
    if self.n_agents == 2:
      with Timer('Monitor Retrieval Time', period=1):
        models, payoffs, counts = ray.get([
          self.parameter_server.get_active_models.remote(), 
          self.parameter_server.get_payoffs.remote(fill_nan=True), 
          self.parameter_server.get_counts.remote()
        ])
      with Timer('Monitor Matrix Plot Time', period=1):
        if self.self_play:
          m = models[0]
          assert payoffs.shape[0] == payoffs.shape[1], payoffs.shape
          assert counts.shape[0] == counts.shape[1], counts.shape
          self.monitors[m].store(payoff=payoffs, count=counts)
          self.plot_stats(
            model=m,
            stats=payoffs, 
            xlabel='Player2', 
            ylabel='Player1', 
            name='payoff', 
            step=step
          )
          self.plot_stats(
            model=m,
            stats=counts, 
            xlabel='Player2', 
            ylabel='Player1', 
            name='count', 
            step=step
          )
          # if payoffs.shape[0] > 1:
          #   alpha_rank = AlphaRank(1000, 5, 0)
          #   rank, mass = alpha_rank.compute_rank(payoffs, is_single_population=True, return_mass=True)
          #   self.plot_stats(
          #     model=m, 
          #     stats=mass, 
          #     xlabel='mass',
          #     ylabel='player',
          #     name='mass',
          #     step=step
          #   )
          #   do_logging(f'AlphaRank at step {step}')
          #   do_logging(rank)
        else:
          for m, p, c in zip(models, payoffs, counts):
            # stats = {}
            # for i, (pp, cc) in enumerate(zip(p, c)):
            #   stats[f'payoff{i}'] = pp
            #   stats[f'count{i}'] = cc

            self.monitors[m].store(payoff=p, count=c)
            self.plot_stats(
              model=m,
              stats=p, 
              xlabel='Player2', 
              ylabel='Player1', 
              name='payoff', 
              step=step
            )
            self.plot_stats(
              model=m,
              stats=c, 
              xlabel='Player2', 
              ylabel='Player1', 
              name='count', 
              step=step
            )

  def retrieve_plot_stats(
    self, 
    model: ModelPath, 
    name: str, 
    stats: np.ndarray, 
    fill_nan=False
  ):
    self.update_recording_stats(model, name, stats, fill_nan=fill_nan)
    kwargs = {
      'stats': self._recording_stats[name][model], 
      'xlabel': 'Step', 
      'ylabel': 'Opponent Distribution', 
      'name': f'realtime_{name}',
    }
    return LoggedStats(model, LoggedType.GRAPH, kwargs)

  def plot_recording_stats(
    self, 
    model: ModelPath, 
    name: str, 
    stats: np.ndarray, 
    fill_nan=False
  ):
    self.update_recording_stats(model, name, stats, fill_nan=fill_nan)
    self.plot_stats(
      model, 
      stats=self._recording_stats[name][model], 
      xlabel='Step', 
      ylabel='Opponent Distribution', 
      name=f'realtime_{name}', 
    )

  def update_recording_stats(
    self, 
    model: ModelPath, 
    stats_name: str, 
    new_stats: np.ndarray, 
    fill_nan: bool=False
  ):
    new_stats = np.reshape(new_stats, (-1, 1)).astype(np.float16)
    hist_stats = self._recording_stats[stats_name]
    if model in hist_stats:
      if new_stats.shape[0] > hist_stats[model].shape[0]:
        pad = ((0, new_stats.shape[0] - hist_stats[model].shape[0]), (0, 0))
        hist_stats[model] = np.pad(hist_stats[model], pad)
      hist_stats[model] = np.concatenate([hist_stats[model], new_stats], -1)
    else:
      hist_stats[model] = new_stats
    
    if fill_nan:
      hist_stats[model] = _fill_nan(hist_stats[model])
    
    return hist_stats
    
  def plot_stats(
    self, 
    model: ModelPath, 
    stats: np.ndarray, 
    xlabel: str, 
    ylabel: str, 
    name: str, 
    step=None, 
  ):
    step = self._env_steps[model] if step is None else step
    matrix_tb_plot(
      self.monitors[model], model, stats, xlabel, ylabel, name, step
    )
