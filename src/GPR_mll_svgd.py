import torch
import time
import numpy as np

from src.models import AffineTransformedDistribution, EqualWeightedMixtureDist
from src.random_gp import RandomGP
from src.util import _handle_input_dimensionality
from src.svgd import SVGD, RBF_Kernel, IMQSteinKernel
from src.abstract import RegressionModel
from config import device

class GPRegressionLearnedSVGD(RegressionModel):

    def __init__(self, train_x, train_t, lr=1e-3, num_iter_fit=10000, prior_factor=0.01, feature_dim=1,
                 weight_prior_std=1.0, bias_prior_std=3.0,
                 covar_module='NN', mean_module='NN', mean_nn_layers=(32, 32), kernel_nn_layers=(32, 32),
                 optimizer='Adam', kernel='RBF', bandwidth=None, num_particles=10,
                 normalize_data=True, random_seed=None):
        """
        Hierarchical bayesian GP Regression with SVGD hyper-posterior

        Args:
            train_x: (ndarray) train inputs - shape: (n_sampls, ndim_x)
            train_t: (ndarray) train targets - shape: (n_sampls, 1)
            lr: (float) learning rate for prior parameters
            num_iter_fit: (int) number of gradient steps for fitting the parameters
            prior_factor: (float) weighting of the hyper-prior (--> meta-regularization parameter)
            feature_dim: (int) output dimensionality of NN feature map for kernel function
            weight_prior_std (float): std of Gaussian hyper-prior on weights
            bias_prior_std (float): std of Gaussian hyper-prior on biases
            covar_module: (gpytorch.mean.Kernel) optional kernel module, default: RBF kernel
            mean_module: (gpytorch.mean.Mean) optional mean module, default: ZeroMean
            mean_nn_layers: (tuple) hidden layer sizes of mean NN
            kernel_nn_layers: (tuple) hidden layer sizes of kernel NN
            optimizer: (str) type of optimizer to use - must be either 'Adam' or 'SGD'
            kernel (std): SVGD kernel, either 'RBF' or 'IMQ'
            bandwidth (float): bandwidth of kernel, if None the bandwidth is chosen via heuristic
            num_particles: (int) number particles to approximate the hyper-posterior
            normalize_data: (bool) whether the data should be normalized
            random_seed: (int) seed for pytorch
        """
        super().__init__(normalize_data=normalize_data, random_seed=random_seed)

        assert kernel in ['RBF', 'IMQ']

        self.num_iter_fit, self.prior_factor, self.feature_dim = num_iter_fit, prior_factor, feature_dim
        self.weight_prior_std, self.bias_prior_std = weight_prior_std, bias_prior_std
        self.num_particles = num_particles

        """ ------ Data handling ------ """
        self.train_x, self.train_t = self._initial_data_handling(train_x, train_t)
        assert self.train_t.shape[-1] == 1
        self.train_t = self.train_t.flatten()

        """ --- Setup model & inference --- """
        self._setup_model_inference(mean_module, covar_module, mean_nn_layers, kernel_nn_layers,
                                    kernel, bandwidth, optimizer, lr)

        self.fitted = False


    def fit(self, valid_x=None, valid_t=None, verbose=True, log_period=1000):
        """
        fits the hyper-posterior particles with SVGD

        Args:
            valid_x: (np.ndarray) validation inputs - shape: (n_samples, ndim_x)
            valid_y: (np.ndarray) validation targets - shape: (n_samples, 1)
            verbose: (boolean) whether to print training progress
            log_period: (int) number of steps after which to print stats
        """

        assert (valid_x is None and valid_t is None) or (isinstance(valid_x, np.ndarray) and isinstance(valid_x, np.ndarray))

        t = time.time()

        for itr in range(1, self.num_iter_fit + 1):

            self.svgd_step(self.train_x, self.train_t)

            # print training stats stats
            if verbose and (itr == 1 or itr % log_period == 0):
                duration = time.time() - t
                t = time.time()

                message = 'Iter %d/%d - Time %.3f sec' % (itr, self.num_iter_fit, duration)

                # if validation data is provided  -> compute the valid log-likelihood
                if valid_x is not None:
                    valid_ll, rmse = self.eval(valid_x, valid_t)
                    message += ' - Valid-LL: %.3f - Valid-RMSE: %.3f' % (valid_ll, rmse)

                self.logger.info(message)

        self.fitted = True


    def predict(self, test_x, return_density=False):
        """
        computes the predictive distribution of the targets p(t|test_x, train_x, train_y)

        Args:
            test_x: (ndarray) query input data of shape (n_samples, ndim_x)
            return_density: (bool) whether to return a density object or a tuple of numpy arrays (pred_mean, pred_std)

        Returns:
            (pred_mean, pred_std) predicted mean and standard deviation corresponding to p(y_test|X_test, X_train, y_train)
        """

        if test_x.ndim == 1:
            test_x = np.expand_dims(test_x, axis=-1)

        with torch.no_grad():
            test_x_normalized = self._normalize_data(test_x)
            test_x = torch.from_numpy(test_x_normalized).float().to(device)

            pred_dist = self.get_pred_dist(self.train_x, self.train_t, test_x)
            pred_dist = AffineTransformedDistribution(pred_dist, normalization_mean=self.y_mean,
                                                        normalization_std=self.y_std)

            pred_dist = EqualWeightedMixtureDist(pred_dist, batched=True)

            if return_density:
                return pred_dist
            else:
                pred_mean = pred_dist.mean.cpu().numpy()
                pred_std = pred_dist.stddev.cpu().numpy()
                return pred_mean, pred_std


    def eval(self, test_x, test_t):
        """
        Computes the average test log likelihood and the rmse on test data

        Args:
            test_x: (ndarray) test input data of shape (n_samples, ndim_x)
            test_t: (ndarray) test target data of shape (n_samples, 1)

        Returns: (avg_log_likelihood, rmse)

        """

        # convert to tensors
        test_x, test_t = _handle_input_dimensionality(test_x, test_t)
        test_t_tensor = torch.from_numpy(test_t).float().flatten().to(device)

        with torch.no_grad():
            pred_dist = self.predict(test_x, return_density=True)
            avg_log_likelihood = pred_dist.log_prob(test_t_tensor) / test_t_tensor.shape[0]
            rmse = torch.mean(torch.pow(pred_dist.mean - test_t_tensor, 2)).sqrt()

            return avg_log_likelihood.cpu().item(), rmse.cpu().item()


    def _setup_model_inference(self, mean_module_str, covar_module_str, mean_nn_layers, kernel_nn_layers,
                               kernel, bandwidth, optimizer, lr):
        assert mean_module_str in ['NN', 'constant']
        assert covar_module_str in ['NN', 'SE']

        """ random gp model """
        self.random_gp = RandomGP(size_in=self.input_dim, prior_factor=self.prior_factor,
                                  weight_prior_std=self.weight_prior_std, bias_prior_std=self.bias_prior_std,
                                  covar_module_str=covar_module_str, mean_module_str=mean_module_str,
                                  mean_nn_layers=mean_nn_layers, kernel_nn_layers=kernel_nn_layers)

        """ SVGD """

        if kernel == 'RBF':
            kernel = RBF_Kernel(bandwidth=bandwidth)
        elif kernel == 'IMQ':
            kernel = IMQSteinKernel(bandwidth=bandwidth)
        else:
            raise NotImplemented

        # sample initial particle locations from prior
        self.particles = self.random_gp.sample_params_from_prior(shape=(self.num_particles, ))

        # setup inference procedure

        if optimizer == 'Adam':
            self.optimizer = torch.optim.Adam([self.particles], lr=lr)
        elif optimizer == 'SGD':
            self.optimizer = torch.optim.SGD([self.particles], lr=lr)
        else:
            raise NotImplementedError('Optimizer must be Adam or SGD')

        self.svgd = SVGD(self.random_gp, kernel, optimizer=self.optimizer)

        """ define svgd step """
        def svgd_step(x_data, y_data):
            # tile data to svi_batch_shape
            x_data = x_data.view(torch.Size((1,)) + x_data.shape).repeat(self.num_particles, 1, 1)
            y_data = y_data.view(torch.Size((1,)) + y_data.shape).repeat(self.num_particles, 1)
            self.svgd.step(self.particles, x_data, y_data)

        """ define predictive dist """
        def get_pred_dist(x_context, y_context, x_valid):
            with torch.no_grad():
                x_context = x_context.view(torch.Size((1,)) + x_context.shape).repeat(self.num_particles, 1, 1)
                y_context = y_context.view(torch.Size((1,)) + y_context.shape).repeat(self.num_particles, 1)
                x_valid = x_valid.view(torch.Size((1,)) + x_valid.shape).repeat(self.num_particles, 1, 1)

                gp_fn = self.random_gp.get_forward_fn(self.particles)
                gp, likelihood = gp_fn(x_context, y_context, train=False)
                pred_dist = likelihood(gp(x_valid))
            return pred_dist

        self.svgd_step = svgd_step
        self.get_pred_dist = get_pred_dist


if __name__ == "__main__":

    """ 1) Generate some training data from GP prior """
    from experiments.data_sim import GPFunctionsDataset

    data_sim = GPFunctionsDataset(random_state=np.random.RandomState(26))
    meta_train_data = data_sim.generate_meta_train_data(n_tasks=1, n_samples=200)

    train_x = meta_train_data[0][0][:50]
    train_y = meta_train_data[0][1][:50]
    test_x = meta_train_data[0][0][50:]
    test_y = meta_train_data[0][1][50:]

    """ 2) train model """

    for prior_factor in [1.0, 0.01, 0.0001]:
        gpr = GPRegressionLearnedSVGD(train_x, train_y, lr=2e-3, prior_factor=prior_factor, covar_module='SE', mean_module='NN',
                                      num_particles=10, num_iter_fit=5000, mean_nn_layers=(16, 16),
                                      kernel_nn_layers=(16, 16), normalize_data=True)

        gpr.fit(valid_x=test_x, valid_t=test_y, log_period=500)

        """ plotting """

        from matplotlib import pyplot as plt
        plt.scatter(train_x, train_y)

        x_plot = np.sort(test_x.flatten()).reshape(-1, 1)
        y_plot, y_std = gpr.predict(x_plot)

        plt.plot(x_plot, y_plot)
        plt.fill_between(x_plot.flatten(), y_plot-y_std, y_plot+y_std, alpha=.5)
        plt.title("prior_factor=%.4f"%prior_factor)
        plt.show()