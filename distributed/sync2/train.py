import ray

from core.tf_config import \
    configure_gpu, configure_precision, silence_tf_logs
from .local.controller import Controller
from utility.ray_setup import sigint_shutdown_ray
from utility.utils import dict2AttrDict, modify_config, set_seed
from utility import yaml_op


def save_config(config, name='config.yaml'):
    yaml_op.save_config(
        config.asdict(), 
        filename='/'.join([
            config['root_dir'], 
            config['model_name'], 
            name
        ])
    )

def save_configs(configs):
    for i, config in enumerate(configs):
        config.n_agents = len(configs)
        save_config(config, name=f'config_p{i}.yaml')

def main(configs):
    if ray.is_initialized():
        ray.shutdown()
    ray.init()
    sigint_shutdown_ray()

    if len(configs) == 1:
        configs = [dict2AttrDict(configs[0], to_copy=True) 
            for _ in range(configs[0].n_agents)]
        for i, c in enumerate(configs):
            modify_config(c, overwrite_existed_only=True, aid=i)

    config = configs[0]
    save_configs(configs)
    
    seed = config.get('seed')
    print('seed', seed)
    set_seed(seed)
    silence_tf_logs()
    configure_gpu(None)
    configure_precision(config.precision)

    controller = Controller(configs[0])
    controller.build_managers(configs)
    controller.pbt_train()

    ray.shutdown()
