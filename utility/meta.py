import tensorflow as tf


def compute_meta_grads_at_single_step(
    meta_tape, 
    eta, 
    grads, 
    vars, 
    out_grads, 
):
    if vars is None:
        eta_grads = meta_tape.gradient(grads, eta, out_grads)
        return eta_grads, out_grads
    else:
        with meta_tape:
            d = tf.reduce_sum([tf.reduce_sum(v * g) for v, g in zip(grads, out_grads)])
        out_grads = [g1 + g2 for g1, g2 in zip(out_grads, meta_tape.gradient(d, vars))]
        eta_grads = meta_tape.gradient(d, eta)
        return eta_grads, out_grads


def compute_meta_gradients(
    meta_tape, 
    meta_loss, 
    grads_list, 
    theta, 
    eta, 
    inner_steps
):
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
        )
        grads.append(new_grads)
    return grads
