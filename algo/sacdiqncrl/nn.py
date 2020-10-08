import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow_probability import distributions as tfd

from core.module import Module, Ensemble
from core.decorator import config
from nn.func import mlp
from algo.sacdiqn.nn import Encoder, Actor, Q, Temperature, SACIQN


class CRL(Module):
    def __init__(self, config, name='crl'):
        super().__init__(name=name)
        self._mlp = mlp(
            **config,
            out_dtype='float32',
            name='crl',
        )
        out_size = config['out_size']
        self._w = tf.Variable(tf.random.uniform((out_size, out_size)))

    def call(self, x):
        z = self._mlp(x)
        return z

    def logits(self, x_anchor, x_pos):
        x_pos = tf.stop_gradient(x_pos)
        Wx = tf.matmul(self._w, tf.transpose(x_pos))
        logits = tf.matmul(x_anchor, Wx)
        logits = logits - tf.reduce_max(logits, axis=-1, keepdims=True)
        logits = .1 * logits
        return logits


def create_components(config, env, **kwargs):
    assert env.is_action_discrete
    action_dim = env.action_dim
    encoder_config = config['encoder']
    actor_config = config['actor']
    q_config = config['q']
    temperature_config = config['temperature']
    if temperature_config['temp_type'] == 'constant':
        temperature = temperature_config['value']
    else:
        temperature = Temperature(temperature_config)
        
    models = dict(
        encoder=Encoder(encoder_config, name='encoder'),
        target_encoder=Encoder(encoder_config, name='target_encoder'),
        actor=Actor(actor_config, action_dim, name='actor'),
        target_actor=Actor(actor_config, action_dim, name='target_actor'),
        crl=CRL(config['crl'], name='crl'),
        # target_crl=CRL(config['crl'], name='target_crl'), # interestingly, using target crl seems to impair the performance
        q=Q(q_config, action_dim, name='q'),
        target_q=Q(q_config, action_dim, name='target_q'),
        temperature=temperature,
    )
    if config['twin_q']:
        models['q2'] = Q(q_config, action_dim, name='q2')
        models['target_q2'] = Q(q_config, action_dim, name='target_q2')

    return models

def create_model(config, env, **kwargs):
    return SACIQN(config=config, env=env, model_fn=create_components, **kwargs, name='saciqncrl')
