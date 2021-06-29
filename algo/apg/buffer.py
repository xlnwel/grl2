import time
import collections
from threading import Lock
import numpy as np

from core.decorator import config
from utility.utils import batch_dicts, standardize
from replay.utils import *
from algo.ppo.buffer import Buffer as PPOBufffer, \
    compute_indices, reshape_to_sample


class Buffer(PPOBufffer):
    def _add_attributes(self):
        super()._add_attributes()
        self._cache = []
        self._batch_idx = 0     # number of batches has been merged into the buffer
        self._train_step = 0

        assert self._n_trajs // self._n_envs * self._n_envs == self._n_trajs, \
            (self._n_trajs, self._n_envs)
        self._n_batch = self._n_trajs // self._n_envs   # #batch expected to received for training

        # rewrite some stats inherited from PPOBuffer
        if self._sample_size:
            assert self._n_trajs * self.N_STEPS % self._sample_size == 0, \
                f'{self._n_trajs} * {self.N_STEPS} % {self._sample_size} != 0'
            size = self._n_trajs * self.N_STEPS // self._sample_size
            logger.info(f'Sample size: {self._sample_size}')
        else:
            size = self._n_trajs * self.N_STEPS
        if self._adv_type == 'vtrace':
            assert self.N_STEPS == self._sample_size, (self.N_STEPS, self._sample_size)
        self._size = size
        self._mb_size = size // self.N_MBS
        self._idxes = np.arange(size)
        self._shuffled_idxes = np.arange(size)
        self._memory = None

        print(f'Batch size: {size}')
        print(f'Mini-batch size: {self._mb_size}')

        # to avoid contention caused by multi-thread parallelism
        self._lock = Lock()

        self._sleep_time = 0.025
        self._sample_wait_time = 0
        self._epoch_idx = 0

    def __getitem__(self, k):
        if self._memory is None:
            raise NotImplementedError
        else:
            return self._memory[k]

    def __contains__(self, k):
        if self._memory is None:
            raise NotImplementedError
        else:
            return k in self._memory

    def good_to_learn(self):
        return True

    def is_full(self):
        return self._batch_idx >= self._n_batch

    def set_train_step(self, train_step):
        self._train_step = train_step
    
    def set_agent(self, agent):
        self._agent = agent

    @property
    def empty(self):
        return self._memory is None

    def reset(self):
        self._memory = None
        self._mb_idx = 0
        self._epoch_idx = 0
        self._sample_wait_time = 0
        self._ready = False

    def add(self):
        """ No need """
        raise NotImplementedError

    def merge(self, data):
        with self._lock:
            self._cache.append(data)
            self._batch_idx += 1

    def wait_to_sample(self):
        while not self.is_full():
            time.sleep(self._sleep_time)
            self._sample_wait_time += self._sleep_time
        
        # assert self._memory is None, self._memory
        with self._lock:
            self._memory = self._cache[-self._n_batch:]
            self._cache = []
            n = self._batch_idx
            self._batch_idx = 0
        self._memory = batch_dicts(self._memory, np.concatenate)
        for v in self._memory.values():
            assert v.shape[0] == self._n_trajs, (v.shape, self._n_trajs)

        self._trajs_dropped = n * self._n_envs - self._n_trajs
        train_step = self._memory.pop('train_step')
        self._policy_version_min_diff = self._train_step - train_step[:, -1].max()
        self._policy_version_max_diff = self._train_step - train_step[:, 0].min()
        self._policy_version_avg_diff = self._train_step - train_step.mean()

    def sample(self):
        if not self._ready:
            self.wait_to_sample()
            self._agent.update_obs_rms(np.concatenate(self['obs']))
            self._agent.update_reward_rms(
                self['reward'], self['discount'])
            self.update('reward', 
                self._agent.normalize_reward(self['reward']), field='all')
            if self._adv_type != 'vtrace':
                self.compute_advantage_return()
            self.reshape_to_sample()
            self._ready = True

        assert self._memory is not None, self._memory
        # TODO: is this shuffle necessary if we don't divide batch into minibatches
        if self._mb_idx == 0:
            np.random.shuffle(self._shuffled_idxes)

        self._mb_idx, self._curr_idxes = compute_indices(
            self._shuffled_idxes, self._mb_idx, 
            self._mb_size, self.N_MBS)

        sample = {k: self._memory[k][self._curr_idxes, 0]
            if k in self._state_keys 
            else self._memory[k][self._curr_idxes] 
            for k in self._sample_keys}

        if self._adv_type != 'vtrace' and self._norm_adv == 'minibatch':
            sample['advantage'] = standardize(
                sample['advantage'], epsilon=self._epsilon)

        if self._mb_idx == 0:
            self._epoch_idx += 1
            if self._epoch_idx == self.N_EPOCHS:
                # resetting here is especially important 
                # if we use tf.data as sampling is done 
                # in a background thread
                self.reset()

        return sample

    def compute_advantage_return(self):
        self._memory['advantage'], self._memory['traj_ret'] = \
            self._compute_advantage_return(
                self._memory['reward'], self._memory['discount'], 
                self._memory['value'], self._memory['last_value'],
                epsilon=self._epsilon)
        # remove the last value
        del self._memory['last_value']

    def reshape_to_store(self):
        """ No need """
        raise NotImplementedError

    def reshape_to_sample(self):
        if self._adv_type == 'vtrace':
            # v-trace is special as the length of obs is sample_size+1
            # and we don't define N_STEP
            self._memory = {k: v.reshape(self._n_trajs, -1, *v.shape[2:]) 
                for k, v in self._memory.items()}
        else:
            self._memory = reshape_to_sample(
                self._memory, self._n_trajs, self.N_STEPS, self._sample_size)

    def get_async_stats(self):
        return {
            'sample_wait_time': self._sample_wait_time,
            'trajs_dropped': self._trajs_dropped,
            'policy_version_min_diff': self._policy_version_min_diff,
            'policy_version_max_diff': self._policy_version_max_diff,
            'policy_version_avg_diff': self._policy_version_avg_diff,
        }


class LocalBuffer:
    @config
    def __init__(self):
        self.reset()

    def is_full(self):
        return self._idx == self.N_STEPS

    def reset(self):
        self._idx = 0
        self._memory = collections.defaultdict(list)

    def add(self, **data):
        for k, v in data.items():
            self._memory[k].append(v)
        self._idx += 1

    def sample(self):
        results = {}

        for k, v in self._memory.items():
            results[k] = v if k == 'last_value' else np.swapaxes(v, 0, 1)

        return results

    def finish(self, last_value=None,
            last_obs=None, last_mask=None):
        """ Add last value to memory. 
        Leave advantage and return computation to the learner 
        """
        assert self._idx == self.N_STEPS, self._idx
        if last_value is not None:
            self._memory['last_value'] = last_value
        if last_obs is not None:
            assert last_mask is not None, 'last_mask is required'
            self._memory['obs'].append(last_obs)
            self._memory['mask'].append(last_mask)