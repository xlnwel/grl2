import os, atexit
import logging
from collections import defaultdict
import numpy as np
import jax.numpy as jnp
from tools.display import print_dict_info

from core.log import do_logging
from core.typing import ModelPath
from tools.utils import isscalar
from tools.timer import get_current_datetime, compute_time_left

logger = logging.getLogger(__name__)


""" Recorder """
class Recorder:
    def __init__(self, model_path: ModelPath=None, record_file='record.txt', max_steps=None):
        self._model_path = model_path
        self._max_steps = max_steps

        record_file = record_file if record_file.endswith('record.txt') \
            else record_file + '/record.txt'
        if model_path is not None:
            recorder_dir = f'{model_path.root_dir}/{model_path.model_name}'
            path = os.path.join(recorder_dir, record_file)
            # if os.path.exists(path) and os.stat(path).st_size != 0:
            #     i = 1
            #     name, suffix = path.rsplit('.', 1)
            #     while os.path.exists(name + f'{i}.' + suffix):
            #         i += 1
            #     pwc(f'Warning: Log file "{path}" already exists!', 
            #         f'Data will be logged to "{name + f"{i}." + suffix}" instead.',
            #         color='magenta')
            #     path = name + f"{i}." + suffix
            if not os.path.isdir(recorder_dir):
                os.makedirs(recorder_dir)
            self.record_path = path
            self._out_file = open(path, 'a')
            atexit.register(self._out_file.close)
            do_logging(f'Record data to "{self._out_file.name}"', logger=logger)
        else:
            self._out_file = None
            do_logging(f'Record directory is not specified; no data will be recorded to the disk',
                logger=logger)

        self._first_row = True
        self._headers = []
        self._current_row = {}
        self._store_dict = defaultdict(list)

        self._start_time = get_current_datetime()
        self._last_time = self._start_time
        self._start_step = 0

    def __contains__(self, item):
        return item in self._store_dict and self._store_dict[item] != []

    def contains_stats(self, item):
        return item in self._store_dict and self._store_dict[item] != []

    def store(self, **kwargs):
        for k, v in kwargs.items():
            if v is None:
                continue
            try:
                if np.any(np.isnan(v)):
                    do_logging(f'{k}: {v}')
                    assert False
            except:
                print(k, v)
            if isinstance(v, (jnp.DeviceArray)):
                v = np.array(v)
            if v is None:
                continue
            elif isinstance(v, (list, tuple)):
                self._store_dict[k] += list(v)
            else:
                self._store_dict[k].append(v)

    def peep_stats_names(self):
        return list(self._store_dict)

    """ All get functions below will remove the corresponding items from the store """
    def get_raw_item(self, key):
        if key in self._store_dict:
            v = self._store_dict[key]
            del self._store_dict[key]
            return v
        return None
        
    def get_item(self, key, mean=True, std=False, min=False, max=False):
        stats = {}
        if key not in self._store_dict:
            return stats
        v = self._store_dict[key]
        if isscalar(v):
            stats[key] = v
            return
        if mean:
            stats[f'{key}'] = np.mean(v).astype(np.float32)
        if std:
            stats[f'{key}_std'] = np.std(v).astype(np.float32)
        if min:
            stats[f'{key}_min'] = np.min(v).astype(np.float32)
        if max:
            stats[f'{key}_max'] = np.max(v).astype(np.float32)
        del self._store_dict[key]
        return stats

    def get_raw_stats(self):
        stats = self._store_dict.copy()
        self._store_dict.clear()
        return stats

    def get_stats(self, mean=True, std=False, min=False, max=False, adaptive=True):
        stats = {}
        for k in sorted(self._store_dict):
            v = self._store_dict[k]
            k_std, k_min, k_max = std, min, max
            if (k.endswith('score') or k.endswith('epslen')) and not k.startswith('metrics/'):
                k = f'metrics/{k}'
            if (
                adaptive 
                and not k.startswith('aux/') 
                and not k.startswith('misc/') 
                and not k.startswith('time/')
                and not k.endswith('std')
                and not k.endswith('min')
                and not k.endswith('max')
            ):
                k_std = k_min = k_max = True
            if isscalar(v):
                stats[k] = v
                continue
            if mean:
                try:
                    if np.any(np.isnan(v)):
                        do_logging(k)
                    stats[k] = np.mean(v).astype(np.float32)
                except:
                    print(k, v)
                    assert False
            if k_std:
                stats[f'{k}_std'] = np.std(v).astype(np.float32)
            if k_min:
                try:
                    stats[f'{k}_min'] = np.min(v).astype(np.float32)
                except:
                    print(k)
                    assert False
            if k_max:
                stats[f'{k}_max'] = np.max(v).astype(np.float32)
        self._store_dict.clear()
        return stats

    def get_count(self, name):
        return len(self._store_dict[name])

    def record_stats(self, stats, print_terminal_info=True):
        if not self._first_row and not set(stats).issubset(set(self._headers)):
            if self._headers and not set(stats).issubset(set(self._headers)):
                do_logging(f'All previous records are erased because stats does not match the first row\n'
                    f'difference = {set(stats) - set(self._headers)}', 
                    logger=logger)
            self._out_file.seek(0)
            self._out_file.truncate()
            self._first_row = True
        [self.record_tabular(k, v) for k, v in stats.items()]
        self.dump_tabular(print_terminal_info=print_terminal_info)

    def _record_tabular(self, key, val):
        """
        Record a value of some diagnostic.

        Call this only once for each diagnostic quantity, each iteration.
        After using ``record_tabular`` to store values for each diagnostic,
        make sure to call ``dump_tabular`` to write them out to file and
        stdout (otherwise they will not get saved anywhere).
        """
        if self._first_row:
            if key not in self._headers:
                self._headers.append(key)
        else:
            assert key in self._headers, \
                f"Trying to introduce a new key {key} " \
                "that you didn't include in the first iteration"
        assert key not in self._current_row, \
            f"You already set {key} this iteration. " \
            "Maybe you forgot to call dump_tabular()"
        self._current_row[key] = val

    def record_tabular(self, key, val=None, mean=True, std=False, min=False, max=False):
        """
        Record a value or possibly the mean/std/min/max values of a diagnostic.
        """
        if val is not None:
            self._record_tabular(key, val)
        else:
            v = np.asarray(self._store_dict[key])
            if mean:
                self._record_tabular(f'{key}_mean', np.mean(v))
            if std:
                self._record_tabular(f'{key}_std', np.std(v))
            if min:
                self._record_tabular(f'{key}_min', np.min(v))
            if max:
                self._record_tabular(f'{key}_max', np.max(v))
        self._store_dict[key] = []

    def dump_tabular(self, print_terminal_info=True):
        """
        Write to disk all the diagnostics from the current iteration.
        """
        def is_print_keys(key):
            return (not key.endswith('std')
                and not key.endswith('max')
                and not key.endswith('min')) and (
                    key.startswith('metrics/') 
                    or key.startswith('run/') 
                    or '/' not in key)

        def get_val_str(val):
            return f"{val:8.3g}" if hasattr(val, "__float__") else str(val)

        vals = []
        key_lens = [len(key) for key in self._headers if is_print_keys(key)]
        val_lens = [len(get_val_str(self._current_row[key]) )
            for key in self._headers if is_print_keys(key)]
        max_key_len = max(15, max(key_lens))
        max_val_len = max(35, max(val_lens))
        n_slashes = 7 + max_key_len + max_val_len
        if print_terminal_info:
            print("-"*n_slashes)
            print(f'| {"model_name":>{max_key_len}s} | {self._model_path.model_name:>{max_val_len}s} |')
        
            steps = self._current_row['steps']
            current_time = get_current_datetime()
            elapsed_time = current_time - self._start_time
            print(f'| {"elapsed_time":>{max_key_len}s} | {str(elapsed_time):>{max_val_len}s} |')
            
            duration = current_time - self._last_time
            elapsed_steps = max(steps - self._start_step, 1)
            if self._max_steps is not None:
                remain_steps = self._max_steps - self._start_step
                time_left = compute_time_left(duration, elapsed_steps, remain_steps)
                time_left = str(time_left).split('.')[0]
                print(f'| {"time_left":>{max_key_len}s} | {time_left:>{max_val_len}s} |')

        for key in self._headers:
            val = self._current_row.get(key, "")
            # print(key, np.array(val).dtype)
            if print_terminal_info and is_print_keys(key):
                valstr = get_val_str(val)
                print(f'| {key:>{max_key_len}s} | {valstr:>{max_val_len}s} |')
            vals.append(val)
        if print_terminal_info:
            print("-"*n_slashes)
        if self._out_file is not None:
            if self._first_row and os.stat(self.record_path).st_size == 0:
                self._out_file.write("\t".join(self._headers)+"\n")
            self._out_file.write("\t".join(map(str,vals))+"\n")
            self._out_file.flush()
        self._current_row.clear()
        self._store_dict.clear()
        self._first_row = False

        self._last_time = current_time
        self._start_step = steps


""" Recorder Ops """
def record_stats(recorder, stats, print_terminal_info=True):
    [recorder.record_tabular(k, v) for k, v in stats.items()]
    recorder.dump_tabular(print_terminal_info=print_terminal_info)

def store(recorder, **kwargs):
    recorder.store(**kwargs)

def get_raw_item(recorder, key):
    return recorder.get_raw_item(key)

def get_item(recorder, key, mean=True, std=False, min=False, max=False):
    return recorder.get_item(key, mean=mean, std=std, min=min, max=max)

def get_raw_stats(recorder):
    return recorder.get_raw_stats()

def get_stats(recorder, mean=True, std=False, min=False, max=False):
    return recorder.get_stats(mean=mean, std=std, min=min, max=max)

def contains_stats(recorder, key):
    return key in recorder

def create_recorder(model_path: ModelPath):
    # recorder save stats in f'{root_dir}/{model_name}/logs/record.txt'
    recorder = Recorder(model_path)
    return recorder
