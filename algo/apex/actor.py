import time
import threading
import functools
import collections
import numpy as np
import tensorflow as tf
import ray

from core.tf_config import *
from core.decorator import config
from utility.display import pwc
from utility.utils import Every
from utility.ray_setup import cpu_affinity
from utility.run import Runner, evaluate, RunMode
from utility import pkg
from env.func import create_env
from core.dataset import process_with_env, RayDataset
    

def get_base_learner_class(BaseAgent):
    class BaseLearner(BaseAgent):            
        def start_learning(self):
            self._learning_thread = threading.Thread(target=self._learning, daemon=True)
            self._learning_thread.start()
            
        def _learning(self):
            while not self.dataset.good_to_learn():
                time.sleep(1)
            pwc(f'{self.name} starts learning...', color='blue')

            while True:
                self.learn_log()
            
        def get_weights(self, name=None):
            return self.model.get_weights(name=name)

        def get_stats(self):
            return self.train_step, super().get_stats()

    return BaseLearner

def get_learner_class(BaseAgent):
    BaseLearner = get_base_learner_class(BaseAgent)
    class Learner(BaseLearner):
        def __init__(self,
                    config, 
                    model_config,
                    env_config,
                    model_fn,
                    replay):
            cpu_affinity('Learner')
            silence_tf_logs()
            configure_threads(config['n_learner_cpus'], config['n_learner_cpus'])
            configure_gpu()
            configure_precision(config.get('precision', 32))

            env = create_env(env_config)
            
            algo = config['algorithm'].split('-', 1)[-1]
            is_per = ray.get(replay.name.remote()).endswith('per')
            n_steps = config['n_steps']
            data_format = pkg.import_module('agent', algo).get_data_format(
                env, is_per, n_steps)
            one_hot_action = config.get('one_hot_action', True)
            process = functools.partial(process_with_env, 
                env=env, one_hot_action=one_hot_action)
            dataset = RayDataset(replay, data_format, process)

            self.model = model_fn(
                config=model_config, 
                env=env)

            super().__init__(
                config=config, 
                models=self.model,
                dataset=dataset,
                env=env,
            )
            
    return Learner


class BaseWorker:
    @config
    def __init__(self,
                 *,
                 worker_id,
                 model_config, 
                 env_config, 
                 buffer_config,
                 model_fn,
                 buffer_fn):
        silence_tf_logs()
        configure_threads(1, 1)
        configure_gpu()
        self._id = worker_id

        self.env = create_env(env_config)
        self.n_envs = self.env.n_envs

        self.model = model_fn( 
            config=model_config, 
            env=self.env)

        self._run_mode = getattr(self, '_run_mode', RunMode.NSTEPS)
        self.runner = Runner(
            self.env, self, 
            nsteps=self.SYNC_PERIOD if self._run_mode == RunMode.NSTEPS else None,
            run_mode=self._run_mode)
        
        assert self._run_mode in [RunMode.NSTEPS, RunMode.TRAJ]
        print(f'{worker_id} action epsilon:', self._act_eps)
        if hasattr(self.model, 'actor'):
            print(f'{worker_id} action inv_temp:', np.squeeze(self.model.actor.act_inv_temp))

    def run(self, learner, replay, monitor):
        while True:
            weights = self._pull_weights(learner)
            self._run(weights, replay)
            self._send_episode_info(monitor)

    def store(self, score, epslen):
        if isinstance(score, (int, float)):
            self._info['score'].append(score)
            self._info['epslen'].append(epslen)
        else:
            self._info['score'] += list(score)
            self._info['epslen'] += list(epslen)

    def _pull_weights(self, learner):
        return ray.get(learner.get_weights.remote(name=self._pull_names))

    def _send_episode_info(self, learner):
        if self._info:
            learner.record_episode_info.remote(self._id, **self._info)
            self._info.clear()

class Worker(BaseWorker):
    def __init__(self, 
                *,
                worker_id,
                config,
                model_config,
                env_config, 
                buffer_config,
                model_fn,
                buffer_fn):
        super().__init__(
            worker_id=worker_id,
            config=config,
            model_config=model_config,
            env_config=env_config,
            buffer_config=buffer_config,
            model_fn=model_fn,
            buffer_fn=buffer_fn
        )

        self._seqlen = buffer_config['seqlen']
        self.buffer = buffer_fn(buffer_config)

        self._is_iqn = 'iqn' in self._algorithm or 'fqf' in self._algorithm
        
        if not hasattr(self, '_pull_names'):
            self._pull_names = [k for k in self.model.keys() if 'target' not in k]
        
        self._info = collections.defaultdict(list)
        self._return_stats = self._worker_side_prioritization \
            or buffer_config.get('max_steps', 0) > buffer_config.get('n_steps', 1)

    def __call__(self, x, **kwargs):
        action = self.model.action(
            tf.convert_to_tensor(x), 
            evaluation=False,
            epsilon=tf.convert_to_tensor(self._act_eps, tf.float32),
            return_stats=self._return_stats)
        action = tf.nest.map_structure(lambda x: x.numpy(), action)
        return action

    def _run(self, weights, replay):
        def collect(env, step, reset, **kwargs):
            self.buffer.add(**kwargs)
            if self.buffer.is_full():
                self._send_data(replay)
        start_step = self.runner.step
        self.model.set_weights(weights)
        end_step = self.runner.run(step_fn=collect)
        return end_step - start_step

    def _send_data(self, replay, buffer=None):
        buffer = buffer or self.buffer
        data = buffer.sample()

        if self._worker_side_prioritization:
            data['priority'] = self._compute_priorities(**data)
        data.pop('q', None)
        data.pop('next_q', None)
        replay.merge.remote(data, data['action'].shape[0])
        buffer.reset()

    def _compute_priorities(self, reward, discount, steps, q, next_q, **kwargs):
        target_q = reward + discount * self._gamma**steps * next_q
        priority = np.abs(target_q - q)
        priority += self._per_epsilon
        priority **= self._per_alpha

        return priority
    
def get_worker_class():
    return Worker


class BaseEvaluator:
    def run(self, learner, monitor):
        step = 0
        if getattr(self, 'RECORD_PERIOD', False):
            to_record = Every(self.RECORD_PERIOD)
        else:
            to_record = lambda x: False 
        while True:
            step += 1
            weights = self._pull_weights(learner)
            self._run(weights, record=to_record(step))
            self._send_episode_info(monitor)

    def _run(self, weights, record):        
        self.model.set_weights(weights)
        score, epslen, video = evaluate(self.env, self, 
            record=record, n=self.N_EVALUATION)
        self.store(score, epslen, video)

    def store(self, score, epslen, video):
        self._info['eval_score'] += score
        self._info['eval_epslen'] += epslen
        if video is not None:
            self._info['video'] = video

    def _pull_weights(self, learner):
        return ray.get(learner.get_weights.remote(name=self._pull_names))

    def _send_episode_info(self, learner):
        if self._info:
            learner.record_episode_info.remote(**self._info)
            self._info.clear()


class Evaluator(BaseEvaluator):
    @config
    def __init__(self, 
                *,
                model_config,
                env_config,
                model_fn):
        silence_tf_logs()
        configure_threads(1, 1)

        env_config.pop('reward_clip', False)
        self.env = env = create_env(env_config)
        self.n_envs = self.env.n_envs

        self.model = model_fn(
                config=model_config, 
                env=env)
        
        if not hasattr(self, '_pull_names'):
            self._pull_names = [k for k in self.model.keys() if 'target' not in k]
        
        self._info = collections.defaultdict(list)

    def __call__(self, x, evaluation=True, **kwargs):
        action = self.model.action(
            tf.convert_to_tensor(x), 
            evaluation=evaluation,
            epsilon=self._eval_act_eps)
        if isinstance(action, tuple):
            if len(action) == 2:
                action, terms = action
                return action.numpy()
            elif len(action) == 3:
                action, ar, terms = action
                return action.numpy(), ar.numpy()
            else:
                raise ValueError(action)
        action = action.numpy()
        
        return action


def get_evaluator_class():
    return Evaluator
