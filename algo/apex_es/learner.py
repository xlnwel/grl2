import time
import threading
import numpy as np
import tensorflow as tf
import ray

from core.ensemble import Ensemble
from core.tf_config import configure_gpu, configure_threads
from utility.display import pwc
from utility.timer import TBTimer
from env.gym_env import create_gym_env
from replay.data_pipline import RayDataset
from algo.apex_es.utils import *


def create_learner(BaseAgent, name, model_fn, replay, config, model_config, env_config, replay_config):
    @ray.remote(num_cpus=1, num_gpus=.1)
    class Learner(BaseAgent):
        """ Interface """
        def __init__(self,
                    name, 
                    model_fn,
                    replay,
                    config, 
                    model_config,
                    env_config):
            # tf.debugging.set_log_device_placement(True)
            configure_threads(1, 1)
            configure_gpu()

            env = create_gym_env(env_config)
            dataset = RayDataset(replay, env.state_shape, env.state_dtype, env.action_shape, env.action_dtype)
            self.model = Ensemble(model_fn, model_config, env.state_shape, env.action_dim, env.is_action_discrete)
            
            self.store_map = {}         # map score to weights
            self.n_accusation = 0

            super().__init__(
                name=name, 
                config=config, 
                models=self.model,
                dataset=dataset,
                env=env,
            )
            
        def start_learning(self):
            self._learning_thread = threading.Thread(target=self._learning, daemon=True)
            self._learning_thread.start()
            
        def get_weights(self, name=None):
            return self.model.get_weights(name=name)

        def record_model(self, score, weights):
            self.best_score = max(self.best_score, score)
            self.store_map[score] = weights
            while self.store_map and self.best_score - min(self.store_map) > self.SLACK:
                remove_worst_weights(self.store_map)
            scores = sorted(self.store_map.keys(), reverse=True)
            pwc(f'{self.name}: current stored scores',
                f'{[f"{x:.3g}" for x in scores]}')

        def _learning(self):
            pwc(f'{self.name} starts learning...', color='blue')
            step = 0
            self.writer.set_as_default()
            while True:
                step += 1
                with TBTimer(f'{self.name} train', 10000, to_log=self.timer):
                    self.learn_log()
                if step % 1000 == 0:
                    self.log(step, print_terminal_info=False)
                    self.save(steps=step, print_terminal_info=False)

    config = config.copy()
    model_config = model_config.copy()
    env_config = env_config.copy()
    replay_config = replay_config.copy()
    
    config['model_name'] = 'learner'
    config['SLACK'] = 10
    # learner only define a env to get necessary env info, 
    # it does not actually interact with env
    env_config['n_workers'] = env_config['n_envs'] = 1

    learner = Learner.remote(name, model_fn, replay, config, 
                            model_config, env_config)
    ray.get(learner.save_config.remote(dict(
        env=env_config,
        model=model_config,
        agent=config,
        replay=replay_config
    )))

    return learner