from functools import wraps
import tensorflow as tf

from utility.display import display_var_info, pwc
from core.checkpoint import setup_checkpoint
from core.log import setup_logger, setup_tensorboard, save_code


""" Functions used to print model variables """                    
def display_model_var_info(models):
    learnable_models = {}
    opts = {}
    nparams = 0
    for name, model in models.items():
        if 'target' in name or name in learnable_models or name in opts:
            pass # ignore variables in the target networks
        elif 'opt' in name:
            opts[name] = model
        else:
            learnable_models[name] = model
    
    pwc(f'Learnable models:', color='yellow')
    for name, model in learnable_models.items():
        nparams += display_var_info(
            model.trainable_variables, name=name, prefix='   ')
    pwc(f'Total learnable model parameters: {nparams*1e-6:0.4g} million', color='yellow')
    

def agent_config(init_fn):
    """ Decorator for agent's initialization """
    def wrapper(self, *, name, config, models, **kwargs):
        """
        Args:
            name: Agent's name
            config: configuration for agent, 
                should be read from config.yaml
            models: a dict of models
            kwargs: optional arguments for each specific agent
        """
        # preprocessing
        # name is used for better bookkeeping, 
        # while model_name is used for create save/log files
        # e.g., all workers share the same name, but with differnt model_names
        self.name = name
        """ For the basic configuration, see config.yaml in algo/*/ """
        [setattr(self, k if k.isupper() else f'_{k}', v) for k, v in config.items()]
        
        pwc(f'{self._model_name.title()} starts constructing...', color='cyan')

        # track models and optimizers for Checkpoint
        self._ckpt_models = {}
        for name_, model in models.items():
            setattr(self, name_, model)
            if isinstance(model, tf.Module) or isinstance(model, tf.Variable):
                self._ckpt_models[name_] = model
                
        # define global steps for train/env step tracking
        self.global_steps = tf.Variable(0, dtype=tf.int64)

        if config.get('writer', True):
            self._writer = setup_tensorboard(self._root_dir, self._model_name)
            tf.summary.experimental.set_step(0)

        # Agent initialization
        init_fn(self, **kwargs)

        if config.get('display_var', True):
            display_model_var_info(self._ckpt_models)

        if config.get('save_code', True):
            save_code(self._root_dir, self._model_name)
        
        self._ckpt, self._ckpt_path, self._ckpt_manager = \
            setup_checkpoint(self._ckpt_models, self._root_dir, 
                            self._model_name, self.global_steps, self._model_name)

        # to save stats to files, specify `logger: True` in config.yaml 
        self._logger = setup_logger(
            config.get('logger', None) and self._root_dir, 
            self._model_name)

        self.print_construction_complete()
    
    return wrapper

def config(init_fn):
    def wrapper(self, config, *args, **kwargs):
        [setattr(self, k if k.isupper() else f'_{k}', v) 
            for k, v in config.items()]

        init_fn(self, *args, **kwargs)

    return wrapper

def override(cls):
    @wraps(cls)
    def check_override(method):
        if method.__name__ not in dir(cls):
            raise NameError("{} does not override any method of {}".format(
                method, cls))
        return method

    return check_override
