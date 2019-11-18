from tensorflow.keras import layers


def get_activation(name):
    """ Return an activation layer """
    if name == 'relu':
        return layers.ReLU
    else:
        raise NotImplementedError(f'Activation named "{name}" is not defined')
        