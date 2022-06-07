import tensorflow as tf

from core.module import Module
from nn.registry import nn_registry
from nn.utils import get_activation
from utility.utils import dict2AttrDict


@nn_registry.register('meta')
class MetaParams(Module):
    def __init__(self, name='meta_params', **config):
        super().__init__(name=name)

        self.config = dict2AttrDict(config)

        for k, v in self.config.items():
            setattr(self, k, v.val)
            if v.init is not None:
                init = v.init * tf.ones(v.shape) if v.get('shape') else v.init
                setattr(self, f'{k}_var', tf.Variable(
                    init, dtype='float32', name=f'meta/{k}'))
                setattr(self, f'{k}_act', get_activation(v.act))

        self.params = list(config)

    def __call__(self, name, inner):
        if inner:
            val = self.config[name]['val']
            if self.config[name]['init'] is None:
                return val
            var = getattr(self, f'{name}_var')
            act = getattr(self, f'{name}_act')
            return val * act(var)
        else:
            val = self.config[name]['outer']
            return val
