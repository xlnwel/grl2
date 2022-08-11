import tensorflow as tf
from core.tf_config import configure_gpu
from nn.func import mlp
from optimizers.rmsprop import RMSprop
from utility.utils import set_seed

configure_gpu(0)
set_seed(0)


def inner(x, meta_tape, inner_opt, eta, l):
    with meta_tape:
        with tf.GradientTape() as inner_tape:
            x = l(x)
            loss = tf.reduce_mean(eta * (x-2)**2)
        grads = inner_tape.gradient(loss, l.variables)
        inner_opt.apply_gradients(zip(grads, l.variables))
    trans_grads = list(inner_opt.get_transformed_grads().values())
    return trans_grads


def compute_meta_grads_at_single_step(
    meta_tape, 
    eta, 
    grads, 
    vars, 
    out_grads, 
    i=None, 
):
    if i == 0:
        eta_grads = meta_tape.gradient(grads, eta, out_grads)
        return eta_grads, out_grads
    else:
        # print('out gradients', out_grads)
        with meta_tape:
            d = tf.reduce_sum([tf.reduce_sum(v * g) for v, g in zip(grads, out_grads)])
        # print('d', d)
        out_grads = [g1 + g2 for g1, g2 in zip(out_grads, meta_tape.gradient(d, vars))]
        eta_grads = meta_tape.gradient(grads, eta, out_grads)
        return eta_grads, out_grads


def compute_meta_gradients(
    meta_tape, 
    meta_loss, 
    grads_list, 
    theta, 
    eta, 
):
    inner_steps = len(grads_list)
    out_grads = meta_tape.gradient(meta_loss, theta)
    grads = []
    for i in reversed(range(inner_steps)):
        # print(i, 'outgrads', out_grads)
        new_grads, out_grads = compute_meta_grads_at_single_step(
            meta_tape, 
            eta, 
            grads_list[i], 
            theta, 
            out_grads, 
            i,
        )
        grads.append(new_grads)
    return grads

from utility.meta import *

meta_opt = RMSprop(1e-3)
inner_opt = RMSprop(1e-3)
meta_tape = tf.GradientTape(persistent=True)
l = mlp([2, 4], out_size=1, activation='relu')
# l = tf.keras.layers.Dense(1)
eta = tf.Variable(1, dtype=tf.float32)

# @tf.function
def outer(x, n):
    grads_list = []
    for _ in range(n):
        grads_list.append(inner(x, meta_tape, inner_opt, eta, l))
    # print('grads list', *grads_list)
    with meta_tape:
        x = l(x)
        # print('x', x)
        loss = tf.reduce_mean((1 - x)**2)

    # print('loss', loss)
    # print(meta_tape.gradient(loss, l.variables))

    grads = compute_meta_gradients(
        meta_tape, 
        loss, 
        grads_list, 
        l.variables,
        eta, 
        # n
    )

    return grads


if __name__ == '__main__':
    x = tf.random.uniform((2, 3))
    print('x', x)
    grads = outer(x, 3)
    print('grads', grads)