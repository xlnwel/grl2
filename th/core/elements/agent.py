import os
from typing import Dict, Union

from th.core.elements.builder import ElementsBuilder
from th.core.elements.strategy import Strategy
from th.core.names import PATH_SPLIT
from th.core.elements.monitor import Monitor
from th.core.typing import ModelPath, get_algo, AttrDict, ModelWeights
from tools.decorator import *
from tools.log import do_logging
from tools.file import search_for_config


class Agent:
  """ Initialization """
  def __init__(
    self, 
    *, 
    config: AttrDict,
    strategy: Union[Dict[str, Strategy], Strategy]=None,
    monitor: Monitor=None,
    name: str=None,
    to_restore=True, 
    builder: ElementsBuilder=None
  ):
    self.config = config
    self._name = name
    self._model_path = ModelPath(config.root_dir, config.model_name)
    if isinstance(strategy, dict):
      self.strategies: Dict[str, Strategy] = strategy
    else:
      self.strategies: Dict[str, Strategy] = {'default': strategy}
    self.strategy: Strategy = next(iter(self.strategies.values()))
    self.monitor: Monitor = monitor
    self.builder: ElementsBuilder = builder
    # trainable is set to align with the first strategy
    self.is_trainable = self.strategy.is_trainable

    if to_restore:
      self.restore()

    self._post_init()
  
  def _post_init(self):
    pass

  @property
  def name(self):
    return self._name

  def reset_model_path(self, model_path: ModelPath):
    self.strategy.reset_model_path(model_path)
    if self.monitor:
      self.monitor.reset_model_path(model_path)

  def get_model_path(self):
    return self._model_path

  def add_strategy(self, sid, strategy: Strategy):
    self.strategies[sid] = strategy

  def switch_strategy(self, sid):
    self.strategy = self.strategies[sid]

  def set_strategy(self, strategy: ModelWeights, *, env=None):
    """
      strategy: strategy is rule-based if the model_name is int, 
      in which case strategy.weights is the config for that strategy 
      initialization. Otherwise, strategy is expected to be 
      learned by RL
    """
    self._model_path = strategy.model
    if len(strategy.model.root_dir.split(PATH_SPLIT)) < 3:
      # the strategy is rule-based if model_name is int(standing for version)
      # for rule-based strategies, we expect strategy.weights 
      # to be the kwargs for the strategy initialization
      algo = strategy.model
      if algo not in self.strategies:
        self.strategies[algo] = \
          self.builder.build_rule_based_strategy(
            env, strategy.weights
          )
      self.monitor.reset_model_path(None)
    else:
      algo = get_algo(strategy.model.root_dir)
      if algo not in self.strategies:
        config = search_for_config(os.path.join(strategy.model))
        self.config = config
        build_func = self.builder.build_training_strategy_from_scratch \
          if self.is_trainable else self.builder.build_acting_strategy_from_scratch
        elements = build_func(
          config=config, 
          env_stats=self.strategy.env_stats, 
          build_monitor=self.monitor is not None
        )
        self.strategies[algo] = elements.strategy
        do_logging(f'Adding new strategy {strategy.model}')
      if self.monitor is not None and self.monitor.save_to_disk:
        self.monitor = self.monitor.reset_model_path(strategy.model)
      self.strategies[algo].set_weights(strategy.weights)

    self.strategy = self.strategies[algo]

  def __getattr__(self, name):
    if name.startswith('_'):
      raise AttributeError(f"Attempted to get missing private attribute '{name}'")
    if hasattr(self.strategy, name):
      # Expose the interface of strategy as Agent and Strategy are interchangeably in many cases 
      return getattr(self.strategy, name)
    elif hasattr(self.monitor, name):
      return getattr(self.monitor, name)
    raise AttributeError(f"Attempted to get missing attribute '{name}'")

  def __call__(self, *args, **kwargs):
    return self.strategy(*args, **kwargs)

  """ Train """
  def train_record(self, **kwargs):
    stats = self.strategy.train(**kwargs)
    self.monitor.store(**stats)
    return stats

  def save(self):
    for s in self.strategies.values():
      s.save()
  
  def restore(self, skip_model=False, skip_actor=False, skip_trainer=False):
    for s in self.strategies.values():
      s.restore(
        skip_model=skip_model, 
        skip_actor=skip_actor, 
        skip_trainer=skip_trainer
      )


def create_agent(**kwargs):
  return Agent(**kwargs)
