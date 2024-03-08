import warnings
import argparse
warnings.filterwarnings("ignore")

import os, sys
os.environ['XLA_FLAGS'] = "--xla_gpu_force_compilation_parallelism=1"

import time
import numpy as np

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.elements.builder import ElementsBuilder
from core.log import do_logging
from core.names import PATH_SPLIT
from core.typing import dict2AttrDict
from core.utils import configure_gpu
from tools.plot import plot_data_dict
from tools.ray_setup import sigint_shutdown_ray
from tools.run import simple_evaluate
from tools.utils import modify_config
from tools import pkg
from env.func import create_env
from run.args import parse_eval_args
from run.utils import search_for_config, search_for_all_configs


def plot(data: dict, outdir: str, figname: str):
  data = {k: np.squeeze(v) for k, v in data.items()}
  data = {k: np.swapaxes(v, 0, 1) if v.ndim == 2 else v for k, v in data.items()}
  plot_data_dict(data, outdir=outdir, figname=figname)

def main(configs, n, render):
  config = dict2AttrDict(configs[0])
  use_ray = config.env.get('n_runners', 0) > 1
  if use_ray:
    import ray
    ray.init()
    sigint_shutdown_ray()

  algo_name = config.algorithm
  env_name = config.env['env_name']

  try:
    make_env = pkg.import_module('env', algo=algo_name, place=-1).make_env
  except Exception as e:
    make_env = None
  
  env = create_env(config.env, env_fn=make_env)

  env_stats = env.stats()

  builder = ElementsBuilder(config, env_stats)
  elements = builder.build_acting_agent_from_scratch(to_build_for_eval=True)
  agent = elements.agent
  print('start evaluation')

  if n < env.n_envs:
    n = env.n_envs
  start = time.time()
  scores, epslens, data, video = simple_evaluate(env, [agent], n, render)

  do_logging(f'After running {n} episodes', color='cyan')
  do_logging(f'\tScore: {np.mean(scores):.3g}\n', color='cyan')
  do_logging(f'\tEpslen: {np.mean(epslens):.3g}\n', color='cyan')
  do_logging(f'\tTime: {time.time()-start:.3g}', color='cyan')

  if use_ray:
    ray.shutdown()

  return scores, epslens, video


def parse_eval_args():
  parser = argparse.ArgumentParser()
  parser.add_argument(
    'directory',
    type=str,
    help='directory where checkpoints and "config.yaml" exist',
    nargs='*')
  parser.add_argument(
    '--render', '-r', 
    action='store_true')
  parser.add_argument(
    '--n_episodes', '-n', 
    type=int, 
    default=1)
  parser.add_argument(
    '--n_envs', '-ne', 
    type=int, 
    default=0)
  parser.add_argument(
    '--n_runners', '-nr', 
    type=int, 
    default=0)
  args = parser.parse_args()

  return args


if __name__ == '__main__':
  args = parse_eval_args()

  # load respective config
  if len(args.directory) == 1:
    configs = search_for_all_configs(args.directory[0])
    directories = [args.directory[0] for _ in configs]
  else:
    configs = [search_for_config(d) for d in args.directory]
    directories = args.directory
  config = configs[0]

  # get the main function
  # try:
  #   main = pkg.import_main('eval', config=config)
  # except Exception as e:
  #   do_logging(f'Default evaluation is used due to error: {e}', color='red')

  configure_gpu()

  # set up env_config
  for d, config in zip(directories, configs):
    if not d.startswith(config.root_dir):
      i = d.find(config.root_dir)
      if i == -1:
        names = d.split(PATH_SPLIT)
        root_dir = os.path.join(n for n in names if n not in config.model_name)
        model_name = os.path.join(n for n in names if n in config.model_name)
        model_name = config.model_name[config.model_name.find(model_name):]
      else:
        root_dir = d[:i] + config.root_dir
        model_name = config.model_name
      do_logging(f'root dir: {root_dir}')
      do_logging(f'model name: {model_name}')
      config = modify_config(
        config, 
        overwrite_existed_only=True, 
        root_dir=root_dir, 
        model_name=model_name
      )
    n = args.n_episodes
    if args.n_runners:
      if 'runner' in config:
        config.runner.n_runners = args.n_runners
      config.env.n_runners = args.n_runners
    if args.n_envs:
      config.env.n_envs = args.n_envs
    n = max(args.n_runners * args.n_envs, n)

  main(configs, n=n, render=args.render)
