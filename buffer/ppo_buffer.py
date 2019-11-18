import numpy as np
from copy import deepcopy

from utility.display import assert_colorize, pwc
from utility.utils import moments, standardize


class PPOBuffer(dict):
    def __init__(self, 
                n_envs, 
                epslen, 
                n_minibatches, 
                state_shape, 
                state_dtype, 
                action_shape, 
                action_dtype,
                **kwargs):
        self.n_envs = n_envs
        self.epslen = epslen
        self.n_minibatches = n_minibatches

        self.indices = np.arange(self.n_envs)
        self.minibatch_size = self.epslen // self.n_minibatches

        # Environment hack
        self.reward_scale = kwargs.get('reward_scale', 1.)
        self.reward_clip = kwargs.get('reward_clip', -float('inf'))
        
        assert_colorize(epslen // n_minibatches * n_minibatches == epslen, 
            f'#envs({n_envs}) is not divisible by #minibatches{n_minibatches}')

        self.basic_shape = (n_envs, epslen)
        super().__init__(
            state=np.zeros((*self.basic_shape, *state_shape), dtype=state_dtype),
            action=np.zeros((*self.basic_shape, *action_shape), dtype=action_dtype),
            reward=np.zeros((*self.basic_shape, 1), dtype=np.float32),
            nonterminal=np.zeros((*self.basic_shape, 1), dtype=np.float32),
            value=np.zeros((n_envs, epslen+1, 1), dtype=np.float32),
            traj_ret=np.zeros((*self.basic_shape, 1), dtype=np.float32),
            advantage=np.zeros((*self.basic_shape, 1), dtype=np.float32),
            old_logpi=np.zeros((*self.basic_shape, 1), dtype=np.float32),
            mask=np.zeros((*self.basic_shape, 1), dtype=np.float32),
        )

        self.reset()

    def add(self, **data):
        assert_colorize(self.idx < self.epslen, 
            f'Out-of-range idx {self.idx}. Call "self.reset" beforehand')
        idx = self.idx

        for k, v in data.items():
            if v is not None:
                self[k][:, idx] = v

        self.idx += 1

    def get_batch(self):
        assert_colorize(self.ready, 
            f'PPOBuffer is not ready to be read. Call "self.finish" first')
        start = self.batch_idx * self.minibatch_size
        end = np.minimum((self.batch_idx + 1) * self.minibatch_size, self.idx)
        if start > self.idx or (end == self.idx and np.sum(self['mask'][:, start:end]) < 500):
            self.batch_idx = 0
            start = self.batch_idx * self.minibatch_size
            end = (self.batch_idx + 1) * self.minibatch_size
        else:
            self.batch_idx = (self.batch_idx + 1) % self.n_minibatches

        keys = ['state', 'action', 'traj_ret', 'value', 
                'advantage', 'old_logpi', 'mask']

        return {k: self[k][:, start:end]
                for k in keys if self[k] is not None}

    def finish(self, last_value, adv_type, gamma, gae_discount):
        self['value'][:, self.idx] = last_value
        valid_slice = np.s_[:, :self.idx]
        self['mask'][:, self.idx:] = 0
        mask = self['mask'][valid_slice]

        # Environment hack
        self['reward'] *= self.reward_scale
        self['reward'] = np.maximum(self['reward'], self.reward_clip)

        if adv_type == 'nae':
            traj_ret = self['traj_ret'][valid_slice]
            next_return = last_value
            for i in reversed(range(self.idx)):
                traj_ret[:, i] = next_return = (self['reward'][:, i] 
                    + self['nonterminal'][:, i] * gamma * next_return)

            # Standardize traj_ret and advantages
            traj_ret_mean, traj_ret_std = moments(traj_ret, mask=mask)
            value = standardize(self['value'][valid_slice], mask=mask)
            # To have the same mean and std as trajectory return
            value = (value + traj_ret_mean) / (traj_ret_std + 1e-8)     
            self['advantage'][valid_slice] = standardize(traj_ret - value, mask=mask)
            self['traj_ret'][valid_slice] = standardize(traj_ret, mask=mask)
        elif adv_type == 'gae':
            advs = delta = (self['reward'][valid_slice] 
                + self['nonterminal'][valid_slice] * gamma * self['value'][:, 1:self.idx+1]
                - self['value'][valid_slice])
            next_adv = 0
            for i in reversed(range(self.idx)):
                advs[:, i] = next_adv = delta[:, i] + self['nonterminal'][:, i] * gae_discount * next_adv
            self['traj_ret'][valid_slice] = advs + self['value'][valid_slice]
            self['advantage'][valid_slice] = standardize(advs, mask=mask)
            # Code for double check 
            # mb_returns = np.zeros_like(mask)
            # mb_advs = np.zeros_like(mask)
            # lastgaelam = 0
            # for t in reversed(range(self.idx)):
            #     if t == self.idx - 1:
            #         nextnonterminal = self['nonterminal'][:, t]
            #         nextvalues = last_value
            #     else:
            #         nextnonterminal = self['nonterminal'][:, t]
            #         nextvalues = self['value'][:, t+1]
            #     delta = self['reward'][:, t] + gamma * nextvalues * nextnonterminal - self['value'][:, t]
            #     mb_advs[:, t] = lastgaelam = delta + gae_discount * nextnonterminal * lastgaelam
            # mb_advs = standardize(mb_advs, mask=mask)
            # assert np.all(np.abs(mb_advs - self['advantage'][valid_slice])<1e-4), f'{mb_advs.flatten()}\n{self["advantage"][valid_slice].flatten()}'
        else:
            raise NotImplementedError

        for k, v in self.items():
            v[valid_slice] = (v[valid_slice].T * mask.T).T
        
        self.ready = True

    def reset(self):
        self.idx = 0
        self.batch_idx = 0
        self.ready = False      # Whether the buffer is ready to be read


if __name__ == '__main__':
    kwargs = dict(
        n_envs=8, 
        epslen=1000, 
        n_minibatches=2, 
        state_shape=[3], 
        state_dtype=np.float32, 
        action_shape=[2], 
        action_dtype=np.float32
    )
    gamma = .99
    gae_discount = gamma * .95

    buffer = PPOBuffer(**kwargs)
    d = np.zeros((kwargs['n_envs'], 1))
    m = np.ones((kwargs['n_envs'], 1))
    diff = kwargs['epslen'] - kwargs['n_envs']
    for i in range(kwargs['epslen']):
        r = np.random.rand(kwargs['n_envs'], 1)
        v = np.random.rand(kwargs['n_envs'], 1)
        if np.random.randint(2):
            d[np.random.randint(kwargs['n_envs'])] = 1
        buffer.add(reward=r,
                value=v,
                nonterminal=1-d,
                mask=m)
        m = 1-d
        if np.all(d == 1):
            break
    last_value = np.random.rand(kwargs['n_envs'], 1)
    buffer.finish(last_value, 'gae', gamma, gae_discount)
    print(buffer['traj_len'][buffer.indices])
    print(buffer['advantage'][buffer.indices, :10, 0])