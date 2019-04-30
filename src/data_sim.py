import numpy as np
import gpytorch
import torch
from numbers import Number
from matplotlib import pyplot as plt

X_LOW = -5
X_HIGH = 5


""" sinusoidal data """

# sinusoid function + gaussian noise
def _sinusoid(x, amplitude=1.0, period=1.0, x_shift=0.0, y_shift=0.0, noise_std=0.0):
    f = amplitude * np.sin(period * (x - x_shift)) + y_shift
    noise = np.random.normal(0, scale=noise_std, size=f.shape)
    return f + noise

def _sample_sinusoid(amp_low=0.2, amp_high=2.0, y_shift_std=0.3, noise_std=0.1):
    assert y_shift_std >= 0 and noise_std >= 0, "std must be non-negative"
    amplitude = np.random.uniform(amp_low, amp_high)
    y_shift = np.random.normal(scale=y_shift_std)
    return lambda x: _sinusoid(x, amplitude=amplitude, y_shift=y_shift, noise_std=noise_std)

def sample_sinusoid_data(size=1, amp_low=0.2, amp_high=2.0, y_shift_std=0.3, noise_std=0.1):
    """ samples a sinusoidal function and then data from the respective function

        Args:
              amp_low (float): min amplitude value
              amp_high (float): max amplitude value
              y_shift_std (float): std of Gaussian from which to sample the y_shift of the sinusoid
              noise_std (float): std of the Gaussian observation noise

        Returns:
            (X, Y): ndarrays of dimensionality (size, 1)
    """

    if isinstance(size, Number):
        size = (int(size),) # convert to tuple

    f = _sample_sinusoid(amp_low=amp_low, amp_high=amp_high, y_shift_std=y_shift_std, noise_std=noise_std)
    X = np.random.uniform(X_LOW, X_HIGH, size=size + (1,))
    Y = f(X)

    assert X.shape[:-1] == Y.shape[:-1] == size # check that simulated data has required size
    assert X.shape[-1] == X.shape[-1] == 1 # check that data is one-dimensional

    return X, Y

if __name__ == "__main__":
    X, Y = sample_sinusoid_data(10)
    print(X.shape)
    plt.scatter(X, Y)
    plt.show()