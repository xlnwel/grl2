import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow.keras.mixed_precision.experimental import global_policy
from tensorflow_probability import distributions as tfd

from utility.display import pwc
from core.module import Module
from core.decorator import config
from nn.func import mlp
from nn.layers import Noisy
from nn.func import cnn
        

class Q(Module):
    @config
    def __init__(self, action_dim, name='q'):
        super().__init__(name=name)
        self._dtype = global_policy().compute_dtype

        self._action_dim = action_dim


        """ Network definition """
        self._cnn = cnn(self._cnn)

        self._v_head = mlp(
            self._v_units, 
            out_dim=1, 
            layer_type=Noisy, 
            activation=self._activation, 
            name='v')
        self._a_head = mlp(
            self._a_units, 
            out_dim=action_dim, 
            layer_type=Noisy, 
            activation=self._activation, 
            name='a')

    def __call__(self, x, deterministic=False, epsilon=0):
        if np.random.uniform() < epsilon:
            size = x.shape[0] if len(x.shape) == 4 else None
            return np.random.randint(self._action_dim, size=size)
        if len(x.shape) == 4:
            x = x / 255.

        x = tf.convert_to_tensor(x, self._dtype)
        x = tf.reshape(x, [-1, tf.shape(x)[-1]])
        noisy = tf.convert_to_tensor(not deterministic, tf.bool)

        action = self.action(x, noisy=noisy, reset=False)
        action = np.squeeze(action.numpy())

        return action

    @tf.function(experimental_relax_shapes=True)
    def action(self, x, noisy=True, reset=True):
        q = self.value(x, noisy=noisy, reset=noisy)
        return tfd.Categorical(q).mode()
    
    @tf.function(experimental_relax_shapes=True)
    def value(self, x, action=None, noisy=True, reset=True):
        if self._cnn:
            x = self._cnn(x)

        v = self._v_head(x, reset=reset, noisy=noisy)
        a = self._a_head(x, reset=reset, noisy=noisy)
        q = v + a - tf.reduce_mean(a, axis=1, keepdims=True)

        if action is not None:
            q = tf.reduce_mean(q * action)
        return q

    def reset_noisy(self):
        self._v_head.reset()
        self._a_head.reset()


def create_model(model_config, action_dim):
    q_config = model_config['q']
    q = Q(q_config, action_dim, 'q')
    target_q = Q(q_config, action_dim, 'target_q')
    return dict(
        actor=q,
        q=q,
        target_q=target_q,
    )
