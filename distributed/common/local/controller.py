import os
import cloudpickle
from functools import partial
import time
from typing import Dict, List, Tuple, Union
import numpy as np
import ray

from core.ckpt.base import YAMLCheckpointBase
from tools import pickle
from core.builder import ElementsBuilderVC
from jx.elements.monitor import Monitor
from core.typing import ModelPath, AttrDict, dict2AttrDict, \
  decompose_model_name, get_basic_model_name, get_date
from core.utils import save_code
from envs.func import get_env_stats
from game.alpharank import AlphaRank
from tools.file import search_for_all_configs, search_for_config
from tools.log import do_logging
from tools.process import run_ray_process
from tools.schedule import PiecewiseSchedule
from tools.timer import Every, Timer, timeit
from tools.utils import batch_dicts, eval_config, modify_config, prefix_name
from tools import yaml_op, pkg
from .agent_manager import AgentManager
from .runner_manager import RunnerManager
from ..remote.monitor import Monitor as RemoteMonitor
from ..remote.parameter_server import ParameterServer
from ..names import *
from ..typing import Status, LoggedType
from ..utils import find_latest_model, find_all_models, matrix_tb_plot


timeit = partial(timeit, period=1)


def _check_configs_consistency(
  configs: List[AttrDict], 
  keys: List[str], 
):
  for key in keys:
    for i, c in enumerate(configs):
      c[key].pop('aid')
      for k in c[key].keys():
        if k != 'root_dir' and k != 'seed':
          assert configs[0][key][k] == c[key][k], (
            key, i, k, c[key][k], configs[0][key][k])


def _setup_configs(
  configs: List[AttrDict], 
  env_stats: List[AttrDict]
):
  configs = [dict2AttrDict(c, to_copy=True) for c in configs]
  for aid, config in enumerate(configs):
    config.aid = aid
    config.buffer.n_envs = env_stats.n_envs
    root_dir = config.root_dir
    model_name = os.path.join(config.model_name, f'a{aid}')
    modify_config(
      config, 
      root_dir=root_dir, 
      model_name=model_name, 
      aid=aid
    )

  return configs


def _build_monitor(model_path: ModelPath, max_steps=None):
  return Monitor(model_path=model_path, name=model_path.model_name, max_steps=max_steps)


class Controller(YAMLCheckpointBase):
  def __init__(
    self, 
    config: AttrDict,
    name='controller',
    to_restore=True,
  ):
    self.config = eval_config(config.controller)
    self.local_save = config.get('local_save', True)
    self.exploiter = config.exploiter   # 是否训练Exploiter
    self.active_models = None           # 当前正在训练的模型
    self.model_path = ModelPath(       # 模型路径
      self.config.root_dir, 
      get_basic_model_name(self.config.model_name)
    )
    self.dir = os.path.join(*self.model_path)
    self.name = name
    self.path = os.path.join(self.dir, f'{name}.yaml')  # 配置储存路径
    do_logging(f'Model Path: {self.model_path}', color='blue')
    save_code(self.model_path)

    self.max_pbt_iterations = self.config.max_pbt_iterations
    # 规划器: 规划每轮迭代与环境交互的次数
    self.iteration_step_scheduler = self._get_iteration_step_scheduler(
      self.max_pbt_iterations, 
      self.config.max_steps_per_iteration
    )
    self._iteration = 1                 # PBT迭代次数
    self._steps = 0                     # 步数
    self.pids = []

    self._status = None
    self.pbt_monitor: Monitor = _build_monitor(self.model_path, self.max_pbt_iterations)
    self.model_monitors: Dict[ModelPath, Monitor] = {
      self.model_path: self.pbt_monitor
    }

    self.pool_pattern = config.get('pool_pattern', '.*')
    self.pool_name = config.get('pool_name', 'strategy_pool')
    date = get_date(config.model_name)
    self.pool_dir = os.path.join(config.root_dir, f'{date}')
    self.pool_path = os.path.join(self.pool_dir, f'{self.pool_name}.yaml')

    if to_restore:
      self.restore()

  """ 构建Managers和Parameter Server """
  def build_managers_for_evaluation(self, config: AttrDict):
    """ 为评估构建Managers和Parameter Server """
    self.self_play = config.self_play
    if config.self_play:
      PSModule = pkg.import_module(
        'remote.parameter_server_sp', config=config)
      PSCls = PSModule.ExploiterSPParameterServer if self.exploiter else PSModule.SPParameterServer
    else:
      PSModule = pkg.import_module(
        'remote.parameter_server', config=config)
      PSCls = PSModule.ExploiterParameterServer if self.exploiter else PSModule.ParameterServer
    self.parameter_server: ParameterServer = \
      PSCls.as_remote().remote(config=config.asdict())

    self.runner_manager: RunnerManager = RunnerManager(
      ray_config=config.ray_config.runner,
      parameter_server=self.parameter_server,
      monitor=None
    )

    self.runner_manager.build_runners(config, evaluation=True)

  def build_managers(self, configs: List[AttrDict]):
    """ 为训练构建Managers和Parameter Server """
    configs = [dict2AttrDict(c) for c in configs]
    _check_configs_consistency(configs, [
      'controller', 
      'parameter_server', 
      'ray_config', 
      'monitor', 
      'runner', 
      # 'env', 
    ])

    do_logging('Retrieving Environment Stats...', color='blue')
    env_stats = get_env_stats(configs[0].env)
    self.suite, _ = configs[0].env.env_name.split('-', 1)
    self.configs = _setup_configs(configs, env_stats)

    config = configs[0]
    self.builder = ElementsBuilderVC(config, env_stats, to_save_code=False)
    self.self_play = config.self_play

    self.n_runners = config.runner.n_runners
    self.n_envs = config.env.n_envs
    self.n_steps = config.runner.n_steps
    self.steps_per_run = self.n_runners * self.n_envs * self.n_steps  # 每轮run与环境的交互次数

    # 导入Parameter Server类
    if config.self_play:
      PSModule = pkg.import_module(
        'remote.parameter_server_sp', config=config)
      PSCls = PSModule.ExploiterSPParameterServer if self.exploiter else PSModule.SPParameterServer
    else:
      PSModule = pkg.import_module(
        'remote.parameter_server', config=config)
      PSCls = PSModule.ExploiterParameterServer if self.exploiter else PSModule.ParameterServer
    do_logging('Building Parameter Server...', color='blue')
    self.parameter_server: ParameterServer = \
      PSCls.as_remote().remote(config=config.asdict())
    # 构建ElementsBuilder
    ray.get(self.parameter_server.build.remote(
      configs=[c.asdict() for c in self.configs],
      env_stats=env_stats.asdict(),
    ))
    self.load_parameter_server()

    # 构建Monitor
    do_logging('Buiding Monitor...', color='blue')
    self.monitor: RemoteMonitor = RemoteMonitor.as_remote().remote(
      config.asdict(), 
      self.parameter_server
    )

    # 构建Agent Manager
    do_logging('Building Agent Manager...', color='blue')
    self.agent_manager: AgentManager = AgentManager(
      ray_config=config.ray_config.agent,
      env_stats=env_stats, 
      parameter_server=self.parameter_server,
      monitor=self.monitor
    )

    # 构建Runner Manager
    do_logging('Building Runner Manager...', color='blue')
    self.runner_manager: RunnerManager = RunnerManager(
      ray_config=config.ray_config.runner,
      parameter_server=self.parameter_server,
      monitor=self.monitor
    )

  """ Training """
  @timeit
  def pbt_train(self):
    while self._iteration <= self.max_pbt_iterations: 
      do_logging(f'Starting Iteration {self._iteration}', color='blue')
      self.initialize_actors()    # 初始化remote actors
      max_steps = self.iteration_step_scheduler(self._iteration) # 当前轮的环境交互次数
      ray.get(self.monitor.set_max_steps.remote(max_steps)) # 设置最大步数
      periods = self.initialize_periods(max_steps)          # 初始化周期
      self._status = Status.TRAINING

      # 开始训练
      self.train(self.agent_manager, self.runner_manager, max_steps, periods)

      # 每轮迭代后的清理
      self.cleanup()
  
    do_logging(f'Training Finished. Total Iterations: {self._iteration-1}', color='blue')

  def _get_iteration_step_scheduler(
    self, 
    max_pbt_iterations: int, 
    max_steps_per_iteration: Union[int, List, Tuple]
  ):
    if isinstance(max_steps_per_iteration, (List, Tuple)):
      iteration_step_scheduler = PiecewiseSchedule(max_steps_per_iteration)
    else:
      iteration_step_scheduler = PiecewiseSchedule([(
        max_pbt_iterations, 
        max_steps_per_iteration
      )])
    return iteration_step_scheduler

  @timeit
  def initialize_actors(self):
    model_weights, is_raw_strategy, configs = ray.get(
      self.parameter_server.sample_training_strategies.remote())
    self.configs = [dict2AttrDict(c, to_copy=True) for c in configs]
    self.configs = self._prepare_configs(self.n_runners, self.n_steps, self._iteration)

    for c in self.configs:
      self.builder.save_config(c)
    self.save_active_models()
    self.active_models = [m.model for m in model_weights]
    do_logging(f'Training Strategies at Iteration {self._iteration}: {self.active_models}', color='blue')

    self.agent_manager.build_agents(self.configs)  # 构建remote agents
    self.agent_manager.set_model_weights(model_weights, wait=True)  # 设置remote agents的模型权重
    self.agent_manager.publish_weights(wait=True) # 发布remote agents的模型权重
    do_logging(f'Finishing Building Agents', color='blue')

    self.active_configs = [
      search_for_config(model, to_attrdict=False) 
      for model in self.active_models
    ]

    # 构建remote runners
    self.runner_manager.build_runners(
      self.configs, 
      remote_buffers=self.agent_manager.get_agents(),
      active_models=self.active_models, 
    )
    do_logging(f'Finishing Building Runners', color='blue')
    self._initialize_rms(self.active_models, is_raw_strategy)

  def initialize_periods(self, max_steps: int):
    periods: Dict[str, Every] = dict2AttrDict({
      'restart_runners': Every(
        self.config.restart_runners_period, 
        0 if self.config.restart_runners_period is None \
          else self._steps + self.config.restart_runners_period
      ), 
      'reload_strategy_pool': Every(
        self.config.reload_strategy_pool_period, start=self._steps, final=max_steps), 
      'eval': Every(self.config.eval_period, start=self._steps, final=max_steps),
      'store': Every(self.config.store_period, start=self._steps, final=max_steps)
    })
    return periods

  @timeit
  def train(
    self, 
    agent_manager: AgentManager, 
    runner_manager: RunnerManager, 
    max_steps: int, 
    periods: Dict[str, Every]
  ):
    raise NotImplementedError()

  @timeit
  def cleanup(self):
    do_logging(f'Cleaning up for Training Iteration {self._iteration}...', color='blue')
    oid = self.monitor.clear_iteration_stats.remote()
    self.save_active_models()
    self.save_archieved_configs()
    self.save_parameter_server()
    self.runner_manager.destroy_runners()
    self.agent_manager.destroy_agents()
    self._iteration += 1
    self.save()
    ray.get(oid)

  """ Implementation for <pbt_train> """
  def _prepare_configs(self, n_runners: int, n_steps: int, iteration: int):
    raise NotImplementedError

  def _initialize_rms(self, models: List[ModelPath], is_raw_strategy: List[bool]):
    if self.config.initialize_rms and any(is_raw_strategy):
      aids = [i for i, is_raw in enumerate(is_raw_strategy) if is_raw]
      do_logging(f'Initializing RMS for Agents: {aids}', color='blue')
      self.runner_manager.set_current_models(models, wait=True)
      self.runner_manager.random_run(aids, wait=True)

  """ Implementation for <train> """
  def _retrieve_model_weights(self):
    model_weights = ray.get(self.parameter_server.get_prepared_strategies.remote())
    while model_weights is None:
      time.sleep(.025)
      model_weights = ray.get(self.parameter_server.get_prepared_strategies.remote())
    assert len(model_weights) == self.n_runners, (len(model_weights), self.n_runners)
    return model_weights

  def _pretrain(
    self, 
    periods: Dict[str, Every], 
    eval_pids: List[ray.ObjectRef]
  ):
    if periods['eval'](self._steps):
      # 评估当前模型
      pid = self._eval(self._steps)
      if pid:
        eval_pids.append(pid)
    if periods['reload_strategy_pool'](self._steps):
      self.load_strategy_pool()
    return eval_pids

  def _posttrain(
    self, 
    periods: Dict[str, Every], 
    eval_pids: List[ray.ObjectRef]
  ):
    if periods['restart_runners'](self._steps):
      do_logging('Restarting Runners', color='blue')
      # 重启remote runners
      self.runner_manager.destroy_runners()
      self.runner_manager.build_runners(
        self.configs, 
        remote_buffers=self.agent_manager.get_agents(),
        active_models=self.active_models, 
      )
    if periods['store'](self._steps):
      if self.local_save:
        stats = ray.get(self.monitor.retrieve_all.remote(self._steps))
        self._log_local(stats)
        self.save_parameter_server()
      else:
        self.monitor.save_all.remote(self._steps)
        self.parameter_server.save_active_models.remote(
          env_step=self._steps, to_print=False)
      self.save()
    if eval_pids:
      ready_pids, eval_pids = ray.wait(eval_pids, timeout=1)
      if self.local_save:
        self._log_local_stats_for_models(
          ready_pids, self.active_models, step=self._steps
        )
      else:
        self._log_remote_stats_for_models(
          ready_pids, self.active_models, step=self._steps
        )

  def _check_scores(self):
    """ 检查score是否满足要求 """
    if self.config.score_threshold is not None:
      scores = ray.get([self.parameter_server.get_avg_score.remote(i, m) 
        for i, m in enumerate(self.agent_manager.models)])
      if all([score > self.config.score_threshold for score in scores]):
        self._status = Status.SCORE_MET
        do_logging(f'Scores have met the threshold: {scores}', color='blue')
        return True
    return False

  def _check_target(self):
    """ 检查Exploiter的目标是否切换 """
    if self.exploiter:
      for model in self.active_models:
        main_model = ModelPath(model.root_dir, model.model_name.replace(EXPLOITER_SUFFIX, ''))
        basic_name, aid = decompose_model_name(main_model.model_name)[:2]
        path = os.path.join(model.root_dir, basic_name, f'a{aid}')
        latest_main = find_latest_model(path)
        if latest_main != main_model:
          do_logging(f'Latest main model has been changed from {main_model} to {latest_main}', color='blue')
          self._status = Status.TARGET_SHIFT
          return True
    return False
  
  def _check_steps(self, steps, max_steps):
    """ 检查是否达到最大交互次数 """
    if steps >= max_steps:
      do_logging(f'The maximum number of steps has been reached', color='blue')
      self._status = Status.TIMEOUT
      return True
    return False

  def _check_termination(self, steps, max_steps):
    """ 当前迭代是否满足终止条件 """
    terminate_flag = self._check_scores()
    if not terminate_flag:
      terminate_flag = self._check_target()
    if not terminate_flag:
      terminate_flag = self._check_steps(steps, max_steps)
    return terminate_flag

  def _finish_iteration(self, eval_pids, **kwargs):
    """ 结束当前迭代, 做相关数据记录 """
    ipid = self._eval(self._iteration)
    do_logging(f'Finishing Iteration {self._iteration}', color='blue')
    if self.local_save:
      stats_list = sum(ray.get([
        self.monitor.retrieve_all.remote(self._steps), 
        self.monitor.retrieve_payoff_table.remote(self._iteration)
      ]), [])
      self._log_local_stats_for_models(
        eval_pids, self.active_models, step=self._steps
      )
      self._log_local(stats_list)
    else:
      ray.get([
        self.monitor.save_all.remote(self._steps),
        self.monitor.save_payoff_table.remote(self._iteration)
      ])
      self._log_remote_stats_for_models(
        eval_pids, self.active_models, step=self._steps
      )
    if ipid:
      stats = ray.get(ipid)
      self.pbt_monitor.store(stats)
      self.pbt_monitor.record(self._iteration, print_terminal_info=True)
    self._steps = 0

  """ Statistics Logging """
  def _log_remote_stats(
    self, 
    pid: ray.ObjectRef, 
    step: int=None, 
    record: bool=False
  ):
    if pid:
      stats = ray.get(pid)
      if step is None:
        step = stats.pop('step')
    else:
      stats = {}
    self.monitor.store_stats.remote(
      stats=stats, 
      step=step, 
      record=record
    )

  def _log_remote_stats_for_models(
    self, 
    pids: List[ray.ObjectRef], 
    models: List[ModelPath], 
    record: bool=False, 
    step: int=None
  ):
    if pids:
      stats_list = ray.get(pids)
      stats = batch_dicts(stats_list)
      stats = prefix_name(stats, name='eval')
    else:
      stats = {}
    for m in models:
      self.monitor.store_stats_for_model.remote(
        m, stats, step=step, record=record)

  def _log_local_stats_for_models(
    self, 
    pids: List[ray.ObjectRef], 
    models: List[ModelPath], 
    record: bool=False, 
    step: int=None
  ):
    if pids:
      stats_list = ray.get(pids)
      stats = batch_dicts(stats_list)
      stats = prefix_name(stats, name='eval')
    else:
      stats = {}
    for m in models:
      if m not in self.model_monitors:
        self.model_monitors[m] = _build_monitor(
          m, max_steps=self.iteration_step_scheduler[self._iteration])
      self.model_monitors[m].record(step=step, stats=stats)

  def _log_local(self, stats_list):
    for model, log_type, stats in stats_list:
      if model not in self.model_monitors:
        self.model_monitors[model] = _build_monitor(
          model, max_steps=self.iteration_step_scheduler(self._iteration))
      if log_type == LoggedType.MONITOR:
        self.model_monitors[model].record(self._steps, stats=stats)
      elif log_type == LoggedType.GRAPH:
        stats['step'] = self._steps
        matrix_tb_plot(self.model_monitors[model], model, **stats)
      else:
        raise NotImplementedError()

  """ Evaluation """
  @timeit
  def evaluate_all(self, total_episodes, filename):
    ray.get(self.parameter_server.reset_payoffs.remote(from_scratch=False))
    self.runner_manager.evaluate_all(total_episodes)
    payoffs = self.parameter_server.get_payoffs.remote()
    counts = self.parameter_server.get_counts.remote()
    payoffs = ray.get(payoffs)
    counts = ray.get(counts)
    if not isinstance(payoffs, list):
      payoffs = [payoffs]
    for i, p in enumerate(payoffs):
      payoffs[i] = np.nan_to_num(p)
    do_logging('Final Payoffs', color='blue')
    for i in range(len(payoffs)):
      print(payoffs[i])
    do_logging(f'Final Counts: {np.mean(counts)}, {np.std(counts)}', color='blue')
    alpha_rank = AlphaRank(1000, 5, 0)
    ranks, mass = alpha_rank.compute_rank(
      payoffs, is_single_population=self.self_play, return_mass=True)
    print('Alpha Rank Results:\n', ranks)
    print('Mass at Stationary Point:\n', mass)
    path = f'{self.dir}/{filename}.pkl'
    with open(path, 'wb') as f:
      cloudpickle.dump((payoffs, counts), f)
    do_logging(f'Payoffs have been saved at {path}', color='blue')

  def _eval(self, step):
    if self.suite == 'spiel':
      pid = self.compute_nash_conv(
        step, avg=True, latest=True, 
        write_to_disk=False, configs=self.active_configs
      )
    else:
      pid = None
    return pid

  def compute_nash_conv(self, step, avg, latest, write_to_disk, configs=None):
    if configs is None:
      configs = search_for_all_configs(self.dir, to_attrdict=False)
    from run.spiel_eval import main
    filename = os.path.join(self.dir, 'nash_conv.txt')
    pid = run_ray_process(
      main, 
      configs, 
      step=step, 
      filename=filename,
      avg=avg, 
      latest=latest, 
      write_to_disk=write_to_disk
    )
    return pid

  """ Checkpoints """
  def load_strategy_pool(self, to_search=True, pattern=None):
    params = {}
    if to_search:
      pattern = pattern or self.pool_pattern
      models = find_all_models(self.pool_dir, pattern)
      for model in models:
        params[model] = pickle.restore_params(model)
      do_logging(f'Loading strategy pool in path {self.pool_dir}', color='green')
    else:
      config = yaml_op.load(self.pool_path)

      for v in config.values():
        for model in v:
          model = ModelPath(*model)
          params[model] = pickle.restore_params(model)
      do_logging(f'Loading strategy pool from {self.pool_path}', color='green')
    ray.get(self.parameter_server.load_params.remote(params))

  def save_active_models(self):
    model_params = ray.get(self.parameter_server.retrieve_active_models.remote())
    for model_param in model_params:
      for model, params in model_param.items():
        pickle.save_hierarchical_params(params, model)

  def load_parameter_server(self):
    payoff_data = pickle.restore(filedir=self.dir, filename=PAYOFF, name=PAYOFF)
    ps_data = pickle.restore(filedir=self.dir, filename=PARAMETER_SERVER, name=PARAMETER_SERVER)
    builder_data = pickle.restore(filedir=self.dir, filename=BUILDERS, name=BUILDERS)
    ray.get(self.parameter_server.load.remote(payoff_data, ps_data, builder_data))
    ray.get(self.parameter_server.add_rule_strategies.remote())
  
  def save_parameter_server(self):
    payoff_data, ps_data, builder_data = ray.get(self.parameter_server.retrieve.remote())
    pickle.save(payoff_data, filedir=self.dir, filename=PAYOFF, name=PAYOFF)
    pickle.save(ps_data, filedir=self.dir, filename=PARAMETER_SERVER, name=PARAMETER_SERVER)
    pickle.save(builder_data, filedir=self.dir, filename=BUILDERS, name=BUILDERS)

  def load_monitor(self):
    data = pickle.restore(filedir=self.dir, filename=MONITOR, name=MONITOR)
    ray.get(self.monitor.load.remote(data))

  def save_monitor(self):
    data = ray.get(self.monitor.retrieve())
    pickle.save(data, filedir=self.dir, filename=MONITOR, name=MONITOR)

  def save_archieved_configs(self):
    configs = ray.get(self.parameter_server.archive_training_strategies.remote(status=self._status))
    for c in configs:
      self.builder.save_config(c)
