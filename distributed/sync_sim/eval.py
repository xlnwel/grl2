import time
import numpy as np
import ray

from .remote.runner_manager import RunnerManager
from utility.ray_setup import sigint_shutdown_ray


def main(configs, n, **kwargs):
    ray.init()
    sigint_shutdown_ray()

    runner = RunnerManager(configs[0].runner)
    runner.build_runners(configs, store_data=False, evaluation=True)
    
    start = time.time()
    stats, n_episodes, video, rewards = runner.evaluate(n)
    duration = time.time() - start

    for f, r in zip(video[-100:], rewards[-100:]):
        print(f)
        print(r)

    config = configs[0]
    n_agents = config.n_agents
    n_workers = config.runner.n_workers
    n = n_episodes - n_episodes % n_workers
    for k, v in stats.items():
        for aid in range(n_agents):
            v = np.array(v[:n])
            pstd = np.std(np.mean(v.reshape(n_workers, -1), axis=-1)) * np.sqrt(n // n_workers)
            print(f'{k} averaged over {n_episodes} episodes: mean({np.mean(v):3g}), std({np.std(v):3g}), pstd({pstd:3g})')
    
    print(f'Evaluation time: total({duration:3g}), average({duration / n_episodes:3g})')

    ray.shutdown()
