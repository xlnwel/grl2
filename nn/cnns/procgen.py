from core.module import Module
from nn.registry import cnn_registry, subsample_registry, block_registry, layer_registry
from nn.utils import *
from utility.tf_utils import get_stoch_state
from nn.cnns.block.ds import State


@cnn_registry.register('procgen')
class ProcgenCNN(Module):
    def __init__(self, 
                 *, 
                 time_distributed=False, 
                 obs_range=[0, 1], 
                #  filters=[16, 32, 32, 32],
                 filters=[32, 64, 64, 64],
                 n_blocks=[1, 1, 1, 1],
                 kernel_initializer='glorot_uniform',
                 stem='conv_maxblurpool',
                 stem_kwargs={},
                 subsample='conv_maxblurpool',
                 subsample_kwargs={},
                 block='resv1',
                 block_kwargs=dict(
                    filter_coefs=[],
                    kernel_sizes=[3, 3],
                    norm=None,
                    norm_kwargs={},
                    activation='relu',
                    am='cbam',
                    am_kwargs={},
                    dropout_rate=0.,
                    rezero=False,
                 ),
                 sa='conv_sa',
                 sa_pos=[],
                 sa_kwargs={},
                 deter_stoch=False,
                 out_activation=None,
                 out_size=None,
                 name='procgen',
                 **kwargs):
        super().__init__(name=name)
        self._obs_range = obs_range
        self._time_distributed = time_distributed
        self._deter_stoch = deter_stoch

        # kwargs specifies general kwargs for conv2d
        kwargs['kernel_initializer'] = get_initializer(kernel_initializer)
        assert 'activation' not in kwargs, kwargs

        stem_cls = subsample_registry.get(stem)
        stem_kwargs.update(kwargs.copy())
        
        block_cls = block_registry.get(block)
        block_kwargs.update(kwargs.copy())

        subsample_cls = subsample_registry.get(subsample)
        subsample_kwargs.update(kwargs.copy())
        assert block_kwargs.get('filters', None) is None, block_kwargs

        sa_cls = block_registry.get(sa)
        sa_kwargs.update(kwargs.copy())

        self._layers = []
        prefix = f'{self.scope_name}/'
        with self.name_scope:
            f_n = zip(filters[:-1], n_blocks[:-1]) if deter_stoch and out_size is None\
                else zip(filters, n_blocks)
            for i, (f, n) in enumerate(f_n):
                subsample_kwargs['filters'] = f
                stem_kwargs['filters'] = f
                self._layers += [
                    stem_cls(name=prefix+stem, **stem_kwargs) if i == 0 and stem is not None else
                    subsample_cls(name=f'{prefix}{subsample}_{i}_f{f}', **subsample_kwargs),
                ] + [block_cls(name=f'{prefix}{block}_{i}_{j}', **block_kwargs) 
                    for j in range(n)]
                if i in sa_pos:
                    self._layers += [
                        sa_cls(name=f'{prefix}{sa}_{i}', **sa_kwargs)
                    ]
            out_act_cls = get_activation(out_activation, return_cls=True)
            self._flat = layers.Flatten(name=prefix+'flatten')
            self.out_size = out_size
            if deter_stoch:
                if out_size is None:
                    f = filters[-1]
                    subsample_kwargs['filters'] = f
                    ds_cls = block_registry.get('dsl')
                    self._ds_layer = ds_cls(n_blocks=n_blocks[-1],
                        subsample=subsample, 
                        subsample_kwargs=subsample_kwargs, 
                        block=block,
                        block_kwargs=block_kwargs,
                        name=f'{prefix}ds')
                    self._layers += [self._ds_layer]
                    self._training_cls.append(ds_cls)
                else:
                    ds_cls = block_registry.get('dss')
                    # NOTE: try adding a relu here 
                    self._dense = self._ds_layer = ds_cls(
                        'dense',
                        self.out_size, 
                        activation=out_act_cls(), 
                        name=prefix+'out')
            else:
                if self.out_size:
                    self._dense = layers.Dense(self.out_size, activation=out_act_cls(), name=prefix+'out')
            self._layers += [out_act_cls(name=prefix+out_activation)]


        self._training_cls += [block_cls, subsample_cls, sa_cls]
    
    @property
    def deter_stoch(self):
        return self._deter_stoch

    def call(self, x, training=False):
        x = convert_obs(x, self._obs_range, global_policy().compute_dtype)
        if self._time_distributed:
            t = x.shape[1]
            x = tf.reshape(x, [-1, *x.shape[2:]])
        x = super().call(x, training=training)
        self.cnn_out = x
        x = self._flat(x)
        if self._time_distributed:
            x = tf.reshape(x, [-1, t, *x.shape[1:]])
        if self.out_size:
            x = self._dense(x)
        if self._deter_stoch:
            self.state = self._ds_layer.state
        return x
