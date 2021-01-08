import functools
import inspect
import tensorflow as tf
from tensorflow.keras import layers


class Module(tf.Module):
    """ This class aims to substitute keras.layers.Layer when 
    encapsulating multiple layers. It provides more fine-grained 
    output for keras.Model.summary.
    Moreover, you can now, without worries about name conflicts,
    define `self._layers`, which is used by default in `call`.
    """
    def __init__(self, name):
        self.scope_name = name
        self._is_built = False
        self._training_cls = [layers.BatchNormalization, layers.Dropout]
        name = name and name.split('/')[-1]
        super().__init__(name=name)

    def __call__(self, x=None, *args, **kwargs):
        if x is not None and not self._is_built:
            if isinstance(x, tf.Tensor):
                self._build(x.shape)
            else:
                self._build(x)
        return self._call(x, *args, **kwargs)
        
    def _build(self, input_shape):
        self.build(input_shape)
        self._is_built = True

    def build(self, *args, **kwargs):
        """ Override this if necessary """
        pass

    # @tf.Module.with_name_scope    # do not decorate with this as it will introduce inconsistent variable names between keras.Model and plain call
    def _call(self, *args, **kwargs):
        return self.call(*args, **kwargs)
        
    def call(self, x, training=False, training_cls=(), **kwargs):
        """ Override this if necessary """
        training_cls = set(training_cls) | set(self._training_cls)
        training_cls = tuple([c.func if isinstance(c, functools.partial) 
            else c for c in training_cls if inspect.isclass(c)])
        
        for l in self._layers:
            if isinstance(l, training_cls):
                x = l(x, training=training)
            else:
                x = l(x, **kwargs)
        return x

    def get_weights(self):
        return [v.numpy() for v in self.variables]

    def set_weights(self, weights):
        [v.assign(w) for v, w in zip(self.variables, weights)]

    def mlp(self, x, *args, name, **kwargs):
        if not hasattr(self, f'_{name}'):
            from nn.func import mlp
            setattr(self, f'_{name}', mlp(*args, name=name, **kwargs))
        return getattr(self, f'_{name}')(x)


class Ensemble:
    """ This class groups all models used by off-policy algorithms together
    so that one can easily get and set all variables """
    def __init__(self, 
                 *,
                 models=None, 
                 config={},
                 model_fn=None, 
                 **kwargs):
        self.models = {}
        for k, v in config.items():
            if not isinstance(v, dict):
                setattr(self, f'{k}', v)

        if models is None:
            self.models = model_fn(config, **kwargs)
        else:
            self.models = models
        [setattr(self, n, m) for n, m in self.models.items()]

    def get_weights(self, name=None):
        """ Return a list/dict of weights
        Returns:
            If name is provided, it returns a dict of weights for models specified by keys.
            Otherwise it returns a list of all weights
        """
        if name is None:
            return [v.numpy() for v in self.variables]
        elif isinstance(name, str):
            name = [name]
        assert isinstance(name, (tuple, list))

        return dict((n, self.models[n].get_weights()) for n in name)

    def set_weights(self, weights):
        """Set weights 
        Args:
            weights: a dict or list of weights. If it's a dict, 
            it sets weights for models specified by the keys.
            Otherwise, it sets all weights 
        """
        if isinstance(weights, dict):
            for n, w in weights.items():
                self[n].set_weights(w)
        else:
            assert len(self.variables) == len(weights)
            [v.assign(w) for v, w in zip(self.variables, weights)]
    
    """ Auxiliary functions that make Ensemble like a dict """
    # def __getattr__(self, key):
    #     if key in self.models:
    #         return self.models[key]
    #     else:
    #         raise ValueError(f'{key} not in models({list(self.models)})')

    def __getitem__(self, key):
        return self.models[key]

    def __setitem__(self, key, value):
        self.models[key] = value
    
    def __contains__(self, item):
        return item in self.models

    def __len__(self):
        return len(self.models)
    
    def __iter__(self):
        return self.models.__iter__()

    def keys(self):
        return self.models.keys()

    def values(self):
        return self.models.values()
    
    def items(self):
        return self.models.items()
