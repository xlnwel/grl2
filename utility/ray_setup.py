import signal
import sys
import psutil
import ray

from utility.display import pwc

def sigint_shutdown_ray():
    """ Shutdown ray when the process is terminated by ctrl+C """
    def handler(sig, frame):
        if ray.is_initialized():
            ray.shutdown()
            pwc('ray has been shutdown by sigint', color='cyan')
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

def cpu_affinity(name=None):
    resources = ray.get_resource_ids()
    if 'CPU' in resources:
        cpus = [v[0] for v in resources['CPU']]
        psutil.Process().cpu_affinity(cpus)
    else:
        cpus = []
        # raise ValueError(f'No cpu is available')
    if name:
        pwc(f'CPUs corresponding to {name}: {cpus}', color='cyan')

def get_num_cpus():
    return len(ray.get_resource_ids()['CPU'])
