import os
import cloudpickle
from typing import Dict, List
import numpy as np

from core.typing import ModelPath, get_aid


class PayoffTable:
    def __init__(
        self, 
        n_agents, 
        step_size, 
        payoff_dir, 
        name='payoff', 
    ):
        self.n_agents = n_agents
        self.step_size = step_size
        self.name = name

        self._dir = payoff_dir
        self._path = f'{self._dir}/{self.name}.pkl'

        self.payoffs = [np.zeros([0] * n_agents, dtype=np.float32) * np.nan for _ in range(n_agents)]
        self.counts = [np.zeros([0] * n_agents, dtype=np.int64) for _ in range(n_agents)]

        self.restore()

    """ Payoff Retrieval """
    def get_payoffs(self, fill_nan=False):
        payoffs = []
        for p in self.payoffs:
            p = p.copy()
            if fill_nan:
                p[np.isnan(p)] = 0
            payoffs.append(p)
        return self.payoffs

    def get_payoffs_for_agent(self, aid: int, *, sid: int=None):
        """ Get the payoff table for agent aid """
        payoff = self.payoffs[aid]
        if sid is not None:
            payoff = payoff[(slice(None), ) * aid + (sid,)]
        assert len(payoff.shape) == self.n_agents - 1, (payoff.shape, self.n_agents)
        return payoff

    def get_counts(self):
        return self.counts

    def get_counts_for_agent(self, aid: int, *, sid: int):
        count = self.counts[aid]
        if sid is not None:
            count = count[(slice(None), ) * aid + (sid)]
        return count

    """ Payoff Management """
    def reset(self, from_scratch=False):
        if from_scratch:
            self.payoffs = [
                np.zeros([0] * self.n_agents, dtype=np.float32) * np.nan
                for _ in range(self.n_agents)
            ]
            self.counts = [
                np.zeros([0] * self.n_agents, dtype=np.int64) 
                for _ in range(self.n_agents)
            ]
        else:
            self.payoffs = [np.zeros_like(p) * np.nan for p in self.payoffs]
            self.counts = [np.zeros_like(c) for c in self.counts]

    def expand(self, aid):
        pad_width = [(0, 0) for _ in range(self.n_agents)]
        pad_width[aid] = (0, 1)
        for i in range(self.n_agents):
            self._expand(i, pad_width)

    def expand_all(self, aids: List[int]):
        assert len(aids) == self.n_agents, aids
        pad_width = [(0, 1) for _ in range(self.n_agents)]
        for aid in enumerate(aids):
            self._expand(aid, pad_width)

    def update(self, sids: List[int], scores: List[List[float]]):
        assert len(sids) == self.n_agents, f'Some models are not specified: {sids}'
        for payoff, count, s in zip(self.payoffs, self.counts, scores):
            s_sum = sum(s)
            s_total = len(s)
            if s == []:
                continue
            elif count[sids] == 0:
                payoff[sids] = s_sum / s_total
            elif self.step_size == 0 or self.step_size is None:
                payoff[sids] = (count[sids] * payoff[sids] + s_sum) / (count[sids] + s_total)
            else:
                payoff[sids] += self.step_size * (s_sum / s_total - payoff[sids])
            assert not np.isnan(payoff[sids]), (count[sids], payoff[sids])
            count[sids] += s_total

    """ Checkpoints """
    def save(self):
        with open(self._path, 'wb') as f:
            cloudpickle.dump((self.payoffs, self.counts), f)

    def restore(self):
        if os.path.exists(self._path):
            with open(self._path, 'rb') as f:
                self.payoffs, self.counts = cloudpickle.load(f)

    """ implementations """
    def _expand(self, aid, pad_width):
        self.payoffs[aid] = np.pad(self.payoffs[aid], pad_width, constant_values=np.nan)
        self.counts[aid] = np.pad(self.counts[aid], pad_width)


class PayoffTableWithModel(PayoffTable):
    def __init__(
        self, 
        n_agents, 
        step_size, 
        payoff_dir, 
        name='payoff', 
    ):
        super().__init__(
            n_agents=n_agents, 
            step_size=step_size, 
            payoff_dir=payoff_dir, 
            name=name
        )

        # mappings between ModelPath and strategy index
        self.model2sid: List[Dict[ModelPath, int]] = [{} for _ in range(n_agents)]
        self.sid2model: List[List[ModelPath]] = [[] for _ in range(n_agents)]

    """ Get & Set """
    def get_all_models(self):
        return self.sid2model

    def get_sid2model(self):
        return self.sid2model
    
    def get_model2sid(self):
        return self.model2sid

    """ Payoff Retrieval """
    def get_payoffs_for_agent(
        self, 
        aid: int, 
        *, 
        sid: int=None, 
        model: ModelPath=None
    ):
        if sid is None and model is not None:
            sid = self.model2sid[aid][model]
        payoff = super().get_payoffs_for_agent(aid, sid=sid)
        return payoff

    def get_counts_for_agent(
        self, 
        aid: int, 
        *, 
        sid: int, 
        model: ModelPath
    ):
        if sid is None and model is not None:
            sid = self.sid2model[aid][model]
        counts = super().get_counts_for_agent(aid, sid=sid)
        return counts

    """ Payoff Management """
    def reset(self, from_scratch=False):
        super().reset(from_scratch)
        if from_scratch:
            self.model2sid: List[Dict[ModelPath, int]] = [{} for _ in range(self.n_agents)]
            self.sid2model: List[List[ModelPath]] = [[] for _ in range(self.n_agents)]

    def expand(self, model: ModelPath, aid=None):
        if aid is None:
            aid = get_aid(model.model_name)
        self._expand_mappings(aid, model)
        super().expand(aid)

        self._check_consistency(aid, model)

    def expand_all(self, models: List[ModelPath]):
        assert len(models) == self.n_agents, models
        pad_width = [(0, 1) for _ in range(self.n_agents)]
        for aid, model in enumerate(models):
            assert aid == get_aid(model.model_name), f'Inconsistent aids: {aid} vs {get_aid(model.model_name)}'
            self._expand_mappings(aid, model)
            self._expand(aid, pad_width)

        for aid, model in enumerate(models):
            self._check_consistency(aid, model)

    def update(
        self, 
        models: List[ModelPath], 
        scores: List[List[float]]
    ):
        assert len(models) == len(scores) == self.n_agents, (models, scores, self.n_agents)
        sids = tuple([
            m2sid[model] for m2sid, model in zip(self.model2sid, models) if model in m2sid
        ])
        super().update(sids, scores)
        # print('Payoffs', *self.payoffs, 'Counts', *self.counts, sep='\n')

    """ Checkpoints """
    def save(self):
        with open(self._path, 'wb') as f:
            cloudpickle.dump(
                (self.payoffs, self.counts, self.model2sid, self.sid2model), f)

    def restore(self):
        if os.path.exists(self._path):
            with open(self._path, 'rb') as f:
                self.payoffs, self.counts, self.model2sid, self.sid2model = cloudpickle.load(f)

    """ Implementations """
    def _expand_mappings(self, aid, model: ModelPath):
        if isinstance(model.model_name, str):
            assert aid == get_aid(model.model_name), (aid, model)
        else:
            assert isinstance(model.model_name, int), model.model_name
        assert model not in self.model2sid[aid], f'Model({model}) is already in {list(self.model2sid[aid])}'
        sid = self.payoffs[aid].shape[aid]
        self.model2sid[aid][model] = sid
        self.sid2model[aid].append(model)
        assert len(self.sid2model[aid]) == sid+1, (sid, self.sid2model)

    def _check_consistency(self, aid, model: ModelPath):
        if isinstance(model.model_name, str):
            assert aid == get_aid(model.model_name), (aid, model)
        else:
            assert isinstance(model.model_name, int), model.model_name
        for i in range(self.n_agents):
            assert self.payoffs[i].shape[aid] == self.model2sid[aid][model] + 1, \
                (self.payoffs[i].shape[aid], self.model2sid[aid][model] + 1)
            assert self.counts[i].shape[aid] == self.model2sid[aid][model] + 1, \
                (self.counts[i].shape[aid], self.model2sid[aid][model] + 1)
