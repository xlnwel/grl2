import argparse
import os, sys
from pathlib import Path
import json
import subprocess
import numpy as np
import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.log import do_logging
from core.mixin.monitor import is_nonempty_file, merge_data
from tools import yaml_op
from tools.utils import flatten_dict, recursively_remove
from tools.logops import *


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',
                        type=str,
                        default='.')
    parser.add_argument('--last_name', '-ln', 
                        type=str, 
                        default=['seed'], 
                        nargs='*')
    parser.add_argument('--name', '-n', 
                        type=str, 
                        default=[], 
                        nargs='*')
    parser.add_argument('--target', '-t', 
                        type=str, 
                        default='~/Documents/html-logs')
    parser.add_argument('--env', '-e', 
                        type=str, 
                        default=None, 
                        nargs='*')
    parser.add_argument('--algo', '-a', 
                        type=str, 
                        default=None, 
                        nargs='*')
    parser.add_argument('--date', '-d', 
                        type=str, 
                        default=None, 
                        nargs='*')
    parser.add_argument('--model', '-m', 
                        type=str, 
                        default=None, 
                        nargs='*')
    parser.add_argument('--n_processes', '-np', 
                        type=int, 
                        default=16)
    parser.add_argument('--sync', 
                        action='store_true')
    parser.add_argument('--ignore', '-i',
                        type=str, 
                        default=[])
    args = parser.parse_args()

    return args


def remove_lists(d):
    to_remove_keys = []
    dicts = []
    for k, v in d.items():
        if isinstance(v, list):
            to_remove_keys.append(k)
        elif isinstance(v, dict):
            dicts.append((k, v))
    for k in to_remove_keys:
        del d[k]
    for k, v in dicts:
        d[k] = remove_lists(v)
    return d


def remove_redundancies(config: dict):
    redundancies = [k for k in config.keys() if k.endswith('id') and '/' in k]
    redundancies += [k for k in config.keys() if k.endswith('algorithm') and '/' in k]
    redundancies += [k for k in config.keys() if k.endswith('env_name') and '/' in k]
    redundancies += [k for k in config.keys() if k.endswith('model_name') and '/' in k]
    for k in redundancies:
        del config[k]
    return config


def rename_env(config: dict):
    if 'env_name' in config:
        env_name = config['env_name']
    else:
        env_name = config['env/env_name']
    suite = env_name.split('-', 1)[0]
    raw_env_name = env_name.split('-', 1)[1]
    config['env_name'] = env_name
    config['env_suite'] = suite
    config['raw_env_name'] = raw_env_name
    return config


def process_data(data, name):
    if 'model_error/ego&train-trans' in data:
        k1_err = data[f'model_error/ego-trans']
        train_err = data[f'model_error/train-trans']
        k1_train_err = np.abs(k1_err - train_err)
        data[f'model_error/ego&train-trans'] = k1_train_err
        data[f'model_error/norm_ego&train-trans'] = np.where(train_err != 0,
            k1_train_err / train_err, k1_train_err)
    if 'cos_lka_pi' in data:
        data['cos_lka_mu_diff'] = data['cos_lka_pi'] - data['cos_mu_pi']
    # if name != 'happo':
    new_data = {}
    for k in data.keys():
        if 'agent0_first_epoch' in k:
            k2 = k.replace('first', 'last')
            new_key = k.split('agent0_first_epoch/') + ['_diff']
            new_key = ''.join(new_key)
            new_data[new_key] = data[k2] - data[k]
    data = pd.concat([data, pd.DataFrame(new_data)], axis=1)
                # print(name)
                # print(k, data.loc[0, k])
                # print(k2, data.loc[0, k2])
                # print(new_key, data.loc[0, new_key])
    # if 'lka_ego_score_diff' in list(data):
    #     data.loc[:, 'lka_ego_score_sign'] = np.sign(data['lka_ego_score_diff'])
    return data

def to_csv(env_name, v):
    SCORE = 'metrics/score'
    if v == []:
        return
    scores = [vv.data[SCORE] for vv in v if SCORE in vv.data]
    if scores:
        scores = np.concatenate(scores)
        max_score = np.max(scores)
        min_score = np.min(scores)
    print(f'env: {env_name}\tmax={max_score}\tmin={min_score}')
    for csv_path, data in v:
        if SCORE in data:
            data[SCORE] = (data[SCORE] - min_score) / (max_score - min_score)
            print(f'\t{csv_path}. norm score max={np.max(data[SCORE])}, min={np.min(data[SCORE])}')
        data.to_csv(csv_path)


if __name__ == '__main__':
    args = parse_args()
    
    config_name = 'config.yaml' 
    player0_config_name = 'config_p0.yaml' 
    js_name = 'parameter.json'
    record_name = 'record'
    progress_name = 'progress.csv'
    name = args.name
    date = get_date(args.date)
    do_logging(f'Loading logs on date: {date}')

    directory = os.path.abspath(args.directory)
    target = os.path.expanduser(args.target)
    sync_dest = os.path.expanduser(args.target)
    do_logging(f'Directory: {directory}')
    do_logging(f'Target: {target}')

    while directory.endswith('/'):
        directory = directory[:-1]
    
    if directory.startswith('/'):
        strs = directory.split('/')
    process = None
    if args.sync:
        old_logs = '/'.join(strs)
        new_logs = f'~/Documents/' + '/'.join(strs[8:])
        if not os.path.exists(new_logs):
            Path(new_logs).mkdir(parents=True)
        cmd = ['rsync', '-avz', old_logs, new_logs, '--exclude', 'src']
        for n in name:
            cmd += ['--include', n]
        do_logging(' '.join(cmd))
        process = subprocess.Popen(cmd)

    search_dir = directory
    level = get_level(search_dir, args.last_name)
    print('Search directory level:', level)

    for d in fixed_pattern_search(
        search_dir, 
        level=level, 
        env=args.env, 
        algo=args.algo, 
        date=date, 
        model=args.model
    ):
        last_name = d.split('/')[-1]
        if not any([last_name.startswith(p) for p in args.last_name]):
            continue
        # load config
        yaml_path = '/'.join([d, config_name])
        if not os.path.exists(yaml_path):
            new_yaml_path = '/'.join([d, player0_config_name])
            if os.path.exists(new_yaml_path):
                yaml_path = new_yaml_path
            else:
                do_logging(f'{yaml_path} does not exist', color='magenta')
                continue
        config = yaml_op.load_config(yaml_path)
        root_dir = config.root_dir
        model_name = config.model_name
        strs = f'{root_dir}/{model_name}'.split('/')
        for s in strs[::-1]:
            if s.endswith('logs'):
                directory = directory.removesuffix(f'/{s}')
                break
            if directory.endswith(s):
                directory = directory.removesuffix(f'/{s}')

        target_dir = d.replace(directory, target)
        do_logging(f'Copy from {d} to {target_dir}')
        if not os.path.isdir(target_dir):
            Path(target_dir).mkdir(parents=True)
        assert os.path.isdir(target_dir), target_dir
        
        # define paths
        json_path = '/'.join([target_dir, js_name])
        record_filename = '/'.join([d, record_name])
        record_path = record_filename + '.txt'
        csv_path = '/'.join([target_dir, progress_name])
        # do_logging(f'yaml path: {yaml_path}')
        if not is_nonempty_file(record_path):
            do_logging(f'Bypass {record_path} due to its non-existence', color='magenta')
            continue
        # save config
        to_remove_keys = ['root_dir', 'seed']
        seed = config.get('seed', 0)
        config = recursively_remove(config, to_remove_keys)
        config['seed'] = seed
        config = remove_lists(config)
        config = flatten_dict(config)
        config = rename_env(config)
        config = remove_redundancies(config)
        config = {k: str(v) for k, v in config.items()}
        # config['model_name'] = config['model_name'].split('/')[1]
        config['buffer/sample_keys'] = []

        # save stats
        data = merge_data(record_filename, '.txt')
        data = process_data(data, config['name'])
        for k in ['expl', 'latest_expl', 'nash_conv', 'latest_nash_conv']:
            if k not in data.keys():
                try:
                    data[k] = (data[f'{k}1'] + data[f'{k}2']) / 2
                except:
                    pass

        with open(json_path, 'w') as json_file:
            json.dump(config, json_file)
        data.to_csv(csv_path, index=False)
        
    if process is not None:
        do_logging('Waiting for rsync to complete...')
        process.wait()

    do_logging('Transfer completed')
