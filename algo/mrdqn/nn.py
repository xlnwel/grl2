import collections
import tensorflow as tf

from utility.rl_utils import epsilon_greedy
from utility.tf_utils import assert_rank
from core.module import Ensemble
from nn.func import Encoder, LSTM
from algo.dqn.nn import Q

LSTMState = collections.namedtuple('LSTMState', ['h', 'c'])

class RDQN(Ensemble):
    def __init__(self, config, env, **kwargs):
        super().__init__(
            model_fn=create_components, 
            config=config,
            env=env,
            **kwargs)
    
    @tf.function
    def action(self, x, state, mask,
            prev_action=None, prev_reward=None,
            evaluation=False, epsilon=0,
            return_stats=False,
            return_eval_stats=False):
        assert x.shape.ndims in (2, 4), x.shape

        x, state = self._encode(
            x, state, mask, prev_action, prev_reward)
        noisy = not evaluation
        q = self.q(x, noisy=noisy, reset=False)
        action = tf.argmax(q, axis=-1, output_type=tf.int32)
        
        eps_action = epsilon_greedy(action, epsilon,
            is_action_discrete=True, action_dim=self.q.action_dim)

        if evaluation:
            return tf.squeeze(eps_action), state
        else:
            eps_prob = epsilon / self.q.action_dim
            terms = {'prob': tf.where(action == eps_action, 1-epsilon+eps_prob, eps_prob)}
            if return_stats:
                terms = {'q': q}
            out = tf.nest.map_structure(lambda x: tf.squeeze(x), (action, terms))
            return out, state

    def _encode(self, x, state, mask, prev_action=None, prev_reward=None):
        x = tf.expand_dims(x, 1)
        mask = tf.expand_dims(mask, 1)
        x = self.encoder(x)
        if hasattr(self, 'rnn'):
            additional_rnn_input = self._process_additional_input(
                x, prev_action, prev_reward)
            x, state = self.rnn(x, state, mask, 
                additional_input=additional_rnn_input)
        else:
            state = None
        return x, state

    def _process_additional_input(self, x, prev_action, prev_reward):
        results = []
        if self.additional_rnn_input:
            if prev_action is not None:
                prev_action = tf.reshape(prev_action, (-1, 1))
                prev_action = tf.one_hot(prev_action, self.actor.action_dim, dtype=x.dtype)
                results.append(prev_action)
            if prev_reward is not None:
                prev_reward = tf.reshape(prev_reward, (-1, 1, 1))
                results.append(prev_reward)
        assert_rank(results, 3)
        return results

def create_components(config, env):
    action_dim = env.action_dim
    encoder_config = config['encoder']
    rnn_config = config['rnn']
    q_config = config['q']
    return dict(
        encoder=Encoder(encoder_config, name='encoder'),
        rnn=LSTM(rnn_config, name='rnn'),
        q=Q(q_config, action_dim, name='q'),
        target_encoder=Encoder(encoder_config, name='target_encoder'),
        target_rnn=LSTM(rnn_config, name='target_rnn'),
        target_q=Q(q_config, action_dim, name='target_q'),
    )

def create_model(config, env, **kwargs):
    return RDQN(config, env, **kwargs)
