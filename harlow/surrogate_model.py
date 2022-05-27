"""Surrogate model (function) module for fitting (not adaptive) and prediction.

`f_surrogate(x) ~= f_target(x)` for `R^n -> R^1` functions.

The main requirements towards each surrogate model are that they:
* can be fitted to points from the target function.
* can make predictions at user selected points.

"""
import re
import warnings
from abc import ABC, abstractmethod

import gpytorch
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
import torch
from gpytorch.mlls import ExactMarginalLogLikelihood, SumMarginalLogLikelihood
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, WhiteKernel
from tensorflow.keras.layers import Dense, Input
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam

tfb = tfp.bijectors
tfd = tfp.distributions
tfk = tfp.math.psd_kernels

# TODO make architectures configurable through API
# TODO chech if class structure is appropriate for all sampling techniques
# TODO add retrainingn strategies


def NLL(y, distr):
    return -distr.log_prob(y)


def root_mean_squared_error(y_true, y_pred):
    return tf.sqrt(tf.reduce_mean(tf.square(y_pred - y_true), axis=1))


def normal_sp(params):
    return tfd.Normal(
        loc=params[:, 0:1], scale=1e-3 + tf.math.softplus(0.05 * params[:, 1:2])
    )  # both parameters are learnable


# TODO add superclass (abstract) check enforcing methods at subclassing
class Surrogate(ABC):
    @abstractmethod
    def create_model(self):
        pass

    @abstractmethod
    def fit(self, X, y):
        pass

    @abstractmethod
    def predict(self, X):
        pass

    @abstractmethod
    def update(self, new_X, new_y):
        pass


class GaussianProcess(Surrogate):
    is_probabilistic = True
    kernel = 1.0 * RBF(1.0) + WhiteKernel(1.0, noise_level_bounds=(5e-5, 5e-2))

    def __init__(self, train_restarts=10, kernel=kernel, **kwargs):
        self.model = None
        self.train_restarts = train_restarts
        self.noise_std = None
        self.kernel = kernel

        self.create_model()

    def create_model(self):
        self.model = GaussianProcessRegressor(
            kernel=self.kernel, n_restarts_optimizer=self.train_restarts, random_state=0
        )

    def fit(self, X, y):
        self.X = X
        self.y = y
        self.model.fit(self.X, self.y)
        self.noise_std = self.get_noise()

    def get_noise(self):
        attrs = list(vars(GaussianProcess.kernel).keys())

        white_kernel_attr = []
        for attr in attrs:
            if re.match(pattern="^k[0-9]", string=attr):
                attr_val = getattr(GaussianProcess.kernel, attr)
                if re.match(pattern="^WhiteKernel", string=str(attr_val)):
                    white_kernel_attr.append(attr)

        if len(white_kernel_attr) == 0:
            raise ValueError(
                "The used kernel should have an additive WhiteKernel component but it was not \
                provided."
            )

        if len(white_kernel_attr) > 1:
            raise ValueError(
                f"The used kernel should have only one additive WhiteKernel component, \
                {len(white_kernel_attr)} components were provided."
            )

        return getattr(self.model.kernel, white_kernel_attr[0]).noise_level ** 0.5

    def predict(self, X, return_std=False):
        samples, std = self.model.predict(X, return_std=True)

        if return_std:
            return samples, std
        else:
            return samples

    def update(self, new_X, new_y):
        X = np.concatenate([self.X, new_X])

        if new_y.ndim > self.y.ndim:
            new_y = new_y.flatten()
        y = np.concatenate([self.y, new_y])

        self.kernel.set_params(**(self.model.kernel_.get_params()))
        self.create_model()

        self.fit(X, y)


class GaussianProcessTFP(Surrogate):
    is_probabilistic = True

    def __init__(self, train_iterations=50, **kwargs):
        self.model = None
        self.train_iterations = train_iterations

        self.create_model()

    def create_model(self):
        def _build_gp(amplitude, length_scale, observation_noise_variance):
            kernel = tfk.ExponentiatedQuadratic(amplitude, length_scale)

            return tfd.GaussianProcess(
                kernel=kernel,
                index_points=self.observation_index_points,
                observation_noise_variance=observation_noise_variance,
            )

        self.gp_joint_model = tfd.JointDistributionNamed(
            {
                "amplitude": tfd.LogNormal(loc=0.0, scale=np.float64(1.0)),
                "length_scale": tfd.LogNormal(loc=0.0, scale=np.float64(1.0)),
                "observation_noise_variance": tfd.LogNormal(
                    loc=0.0, scale=np.float64(1.0)
                ),
                "observations": _build_gp,
            }
        )

    def optimize_parameters(self, verbose=0):
        constrain_positive = tfb.Shift(np.finfo(np.float64).tiny)(tfb.Exp())

        self.amplitude_var = tfp.util.TransformedVariable(
            initial_value=1.0,
            bijector=constrain_positive,
            name="amplitude",
            dtype=np.float64,
        )

        self.length_scale_var = tfp.util.TransformedVariable(
            initial_value=1.0,
            bijector=constrain_positive,
            name="length_scale",
            dtype=np.float64,
        )

        self.observation_noise_variance_var = tfp.util.TransformedVariable(
            initial_value=1.0,
            bijector=constrain_positive,
            name="observation_noise_variance_var",
            dtype=np.float64,
        )

        trainable_variables = [
            v.trainable_variables[0]
            for v in [
                self.amplitude_var,
                self.length_scale_var,
                self.observation_noise_variance_var,
            ]
        ]

        optimizer = tf.optimizers.Adam(learning_rate=0.01)

        @tf.function(autograph=False)
        def train_model():
            with tf.GradientTape() as tape:
                loss = -self.target_log_prob(
                    self.amplitude_var,
                    self.length_scale_var,
                    self.observation_noise_variance_var,
                )
            grads = tape.gradient(loss, trainable_variables)
            optimizer.apply_gradients(zip(grads, trainable_variables))

            return loss

        lls_ = np.zeros(self.train_iterations)
        for i in range(self.train_iterations):
            loss = train_model()
            lls_[i] = loss

        self.kernel = tfk.ExponentiatedQuadratic(
            self.amplitude_var, self.length_scale_var
        )
        if verbose == 1:
            print("Trained parameters:")
            print(f"amplitude: {self.amplitude_var._value().numpy()}")
            print(f"length_scale: {self.length_scale_var._value().numpy()}")
            print(
                "observation_noise_variance: "
                f"{self.observation_noise_variance_var._value().numpy()}"
            )

    def target_log_prob(self, amplitude, length_scale, observation_noise_variance):
        return self.gp_joint_model.log_prob(
            {
                "amplitude": amplitude,
                "length_scale": length_scale,
                "observation_noise_variance": observation_noise_variance,
                "observations": self.observations,
            }
        )

    def fit(self, X, y):
        self.observation_index_points = X
        self.observations = y
        self.optimize_parameters()

    def predict(self, X, iterations=50, return_std=False, return_samples=False):
        gprm = tfd.GaussianProcessRegressionModel(
            kernel=self.kernel,
            index_points=X,
            observation_index_points=self.observation_index_points,
            observations=self.observations,
            observation_noise_variance=self.observation_noise_variance_var,
            predictive_noise_variance=0.0,
        )

        samples = gprm.sample(iterations)

        if return_samples:
            if return_std:
                return (
                    np.mean(samples, axis=0),
                    np.std(samples, axis=0),
                    samples.numpy(),
                )
            else:
                return np.mean(samples, axis=0), samples.numpy()
        if return_std:
            return np.mean(samples, axis=0), np.std(samples, axis=0)
        else:
            return np.mean(samples, axis=0)

    def update(self, new_X, new_y):
        self.observation_index_points = np.concatenate(
            [self.observation_index_points, new_X]
        )

        if new_y.ndim > self.observations.ndim:
            new_y = new_y.flatten()
        self.observations = np.concatenate([self.observations, new_y])
        self.optimize_parameters(verbose=False)


class ExactGPModel(gpytorch.models.ExactGP):
    """
    From: https://docs.gpytorch.ai/en/stable/examples/03_Multitask_Exact_GPs/ModelList_GP_Regression.html # noqa: E501
    """

    def __init__(self, train_x, train_y, likelihood):
        super().__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.ConstantMean()
        self.covar_module = gpytorch.kernels.ScaleKernel(gpytorch.kernels.RBFKernel())

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)


class MultitaskGPModel(gpytorch.models.ExactGP):
    """
    From: https://docs.gpytorch.ai/en/stable/examples/03_Multitask_Exact_GPs/Multitask_GP_Regression.html # noqa: E501
    """

    def __init__(self, train_x, train_y, likelihood, N_tasks):
        super(MultitaskGPModel, self).__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.MultitaskMean(
            gpytorch.means.ConstantMean(), num_tasks=N_tasks
        )
        self.covar_module = gpytorch.kernels.MultitaskKernel(
            gpytorch.kernels.RBFKernel(), num_tasks=N_tasks, rank=1
        )

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultitaskMultivariateNormal(mean_x, covar_x)


class BatchIndependentMultitaskGPModel(gpytorch.models.ExactGP):
    """
    From: https://docs.gpytorch.ai/en/stable/examples/03_Multitask_Exact_GPs/Batch_Independent_Multioutput_GP.html  # noqa: E501
    """

    def __init__(self, train_x, train_y, likelihood, N_tasks):
        super().__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.ConstantMean(
            batch_shape=torch.Size([N_tasks])
        )
        self.covar_module = gpytorch.kernels.ScaleKernel(
            gpytorch.kernels.RBFKernel(batch_shape=torch.Size([N_tasks])),
            batch_shape=torch.Size([N_tasks]),
        )

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultitaskMultivariateNormal.from_batch_mvn(
            gpytorch.distributions.MultivariateNormal(mean_x, covar_x)
        )


class ModelListGaussianProcess(Surrogate):
    """
    Utility class to generate a surrogate composed of multiple independent
    Gaussian processes. Currently uses GPyTorch.

    It is assumed that the training inputs are common between the N_task GPs,
    but not all features are used in each GP.

    Notes:
        * This model must be initialized with data
        * The `.fit(X, y)` method replaces the current `train_X` and `train_y`
        with its arguments every time it is called.
        * The `.update(X, y)` method will append the new X and y training tensors
        to the existing `train_X`, `train_y` and perform the fitting.
        * Both `.fit()` and `.update()` will re-instantiate a new model. There
        is likely a better solution to this.

    TODO:
    * GpyTorch probably has existing functionality for updating and refitting
    models. This is likely the prefered approach and should replace the current
    approach where the model is redefined at each call to `fit()` or `.update()`.
    * Rewrite to use the existing Gaussian process surrogate class
    * Add type hinting
    * Improve docstrings
    """

    is_probabilistic = True

    def __init__(
        self,
        train_X,
        train_y,
        model_names,
        training_max_iter=100,
        learning_rate=0.1,
        min_loss_rate=None,
        optimizer=None,
        mean=None,
        covar=None,
        list_params=None,
        show_progress=True,
        silence_warnings=False,
    ):

        self.model_names = model_names
        self.model = None
        self.train_X = train_X
        self.train_y = train_y
        self.training_max_iter = training_max_iter
        self.noise_std = None
        self.likelihood = None
        self.mll = None
        self.learning_rate = learning_rate
        self.min_loss_rate = min_loss_rate
        self.mean_module = mean
        self.covar_module = covar
        self.list_params = list_params
        self.optimizer = optimizer
        self.show_progress = show_progress
        self.prediction = None

        # Check input consistency
        if self.model_names is None:
            raise ValueError("An iterable of model names must be specified")

        if self.list_params is None:
            raise ValueError(
                "An iterable of parameter indices per model must" "be specified"
            )

        if self.optimizer is None:
            warnings.warn("No optimizer specified, using default.", UserWarning)

        # Silence torch and numpy warnings (related to converting
        # between np.arrays and torch.tensors).
        if silence_warnings:
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)

            # Initialize model
        self.create_model()

    def create_model(self):

        # Reset optimizer
        self.optimizer = None

        # Check input consistency
        if self.train_y.shape[0] != self.train_X.shape[0]:
            raise ValueError(
                f"Dim 0 of `train_y` must be equal to the number of training"
                f"samples but is {self.train_y.shape[0]} != {self.train_X.shape[0]}."
            )

        if self.train_y.shape[1] != len(self.model_names):
            raise ValueError(
                f"Dim 1 of `train_y` must be equal to the number of models"
                f"but is {self.train_y.shape[0]} != {len(self.model_names)}"
            )

        # Assemble the models and likelihoods
        list_likelihoods = []
        list_surrogates = []
        for i, _name in enumerate(self.model_names):

            # Initialize list of GP surrogates
            list_likelihoods.append(gpytorch.likelihoods.GaussianLikelihood())
            list_surrogates.append(
                ExactGPModel(
                    self.train_X[:, self.list_params[i]],
                    self.train_y[:, i],
                    list_likelihoods[i],
                )
            )

            # Collect the independent GPs in ModelList and LikelihoodList objects
            self.model = gpytorch.models.IndependentModelList(*list_surrogates)
            self.likelihood = gpytorch.likelihoods.LikelihoodList(*list_likelihoods)

    def fit(self, train_X=None, train_y=None):

        # If arguments are passed, delete the previously storred `train_X` and `train_y`
        if (train_X is None) or (train_y is None):
            warnings.warn(
                "Calling `.fit()` with no input argumentss will re-instantiate "
                "and fit the model for the currently stored training data."
            )
        else:
            self.train_X = torch.atleast_2d(torch.tensor(train_X).float())
            self.train_y = torch.atleast_2d(torch.tensor(train_y).float())

        # Create model
        self.create_model()

        # Switch the model to train mode
        self.model.train()
        self.likelihood.train()

        # Define optimizer
        if self.optimizer is None:
            self.optimizer = torch.optim.Adam(
                self.model.parameters(), lr=self.learning_rate
            )  # Includes GaussianLikelihood parameters

        # Define marginal loglikelihood
        self.mll = SumMarginalLogLikelihood(self.likelihood, self.model)

        # Train
        self.vec_loss = []
        loss_0 = np.inf
        for _i in range(self.training_max_iter):

            self.optimizer.zero_grad()
            output = self.model(*self.model.train_inputs)
            loss = -self.mll(output, self.model.train_targets)
            loss.backward()
            self.vec_loss.append(loss.item())
            self.optimizer.step()

            # TODO: Will this work for negative losss? CHECK
            loss_ratio = None
            if self.min_loss_rate:
                loss_ratio = (loss_0 - loss.item()) - self.min_loss_rate * loss.item()

            # From https://stackoverflow.com/questions/5290994
            # /remove-and-replace-printed-items
            if self.show_progress:
                print(
                    f"Iter = {_i} / {self.training_max_iter},"
                    f" Loss = {loss.item()}, Loss_ratio = {loss_ratio}",
                    end="\r",
                    flush=True,
                )

            # Get noise value
            self.noise_std = self.get_noise()

            # Check criterion and break if true
            if self.min_loss_rate:
                if loss_ratio < 0.0:
                    break

            # Set previous iter loss to current
            loss_0 = loss.item()

        print(
            f"Iter = {self.training_max_iter},"
            f" Loss = {loss.item()}, Loss_ratio = {loss_ratio}"
        )

    def predict(self, X_pred, return_std=False):

        # Cast input to tensor for compatibility with sampling algorithms
        X_pred = torch.atleast_2d(torch.tensor(X_pred).float())

        # Switch the model to eval mode
        self.model.eval()
        self.likelihood.eval()

        # Get input features per model
        X_list = [X_pred[:, prm_list] for prm_list in self.list_params]

        # Make prediction
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            self.prediction = self.likelihood(*self.model(*X_list))

        # Generate output for each model
        self.mean = torch.zeros(X_pred.shape[0], len(self.model_names))
        self.cr_l = torch.zeros(X_pred.shape[0], len(self.model_names))
        self.cr_u = torch.zeros(X_pred.shape[0], len(self.model_names))
        self.std = torch.zeros(X_pred.shape[0], len(self.model_names))
        self.var = torch.zeros(X_pred.shape[0], len(self.model_names))
        sample = torch.zeros(X_pred.shape[0], len(self.model_names))

        for j, (_submodel, _prediction) in enumerate(
            zip(self.model.models, self.prediction)
        ):

            # Get mean, variance and std. dev per model
            self.mean[:, j] = _prediction.mean
            self.var[:, j] = _prediction.variance
            self.std[:, j] = _prediction.variance.sqrt()

            # Get posterior predictive samples per model
            sample[:, j] = _prediction.sample()

            self.cr_l[:, j], self.cr_u[:, j] = _prediction.confidence_region()

        if return_std:
            return sample, self.std
        else:
            return sample

    def sample_posterior(self, n_samples=1):

        # Switch the model to eval mode
        self.model.eval()
        self.likelihood.eval()

        return [prediction.n_sample(n_samples) for prediction in self.prediction]

    def update(self, new_X, new_y):
        full_X = torch.cat([self.train_X, new_X], dim=0)
        full_y = torch.cat([self.train_y, new_y], dim=0)

        self.optimizer = None
        self.fit(full_X, full_y)

    def get_noise(self):
        return [
            likelihood.noise.sqrt() for likelihood in self.model.likelihood.likelihoods
        ]


class BatchIndependentGaussianProcess(Surrogate):
    """
    !!!!!!!!!!!! IN PROGRESS !!!!!!!!!!!!!!!

    Utility class to generate a surrogate composed of multiple independent
    Gaussian processes with the same covariance and likelihood:
    https://docs.gpytorch.ai/en/stable/examples/03_Multitask_Exact_GPs/Batch_Independent_Multioutput_GP.html

    Notes:
        * This model must be initialized with data
        * The `.fit(X, y)` method replaces the current `train_X` and `train_y`
        with its arguments every time it is called.
        * The `.update(X, y)` method will append the new X and y training tensors
        to the existing `train_X`, `train_y` and perform the fitting.
        * Both `.fit()` and `.update()` will re-instantiate a new model. There
        is likely a better solution to this.

    TODO:
    * GpyTorch probably has existing functionality for updating and refitting
    models. This is likely the prefered approach and should replace the current
    approach where the model is redefined at each call to `fit()` or `.update()`.
    * Rewrite to use the existing Gaussian process surrogate class
    * Add type hinting
    * Improve docstrings
    """

    is_probabilistic = True

    def __init__(
        self,
        train_X,
        train_y,
        num_tasks,
        training_max_iter=100,
        learning_rate=0.1,
        min_loss_rate=None,
        optimizer=None,
        mean=None,
        covar=None,
        show_progress=True,
        silence_warnings=False,
    ):

        self.model = None
        self.train_X = train_X
        self.train_y = train_y
        self.num_tasks = num_tasks
        self.training_max_iter = training_max_iter
        self.noise_std = None
        self.likelihood = None
        self.mll = None
        self.learning_rate = learning_rate
        self.min_loss_rate = min_loss_rate
        self.mean_module = mean
        self.covar_module = covar
        self.optimizer = optimizer
        self.show_progress = show_progress
        self.predictions = None

        if self.optimizer is None:
            warnings.warn("No optimizer specified, using default.", UserWarning)

        # Silence torch and numpy warnings (related to converting
        # between np.arrays and torch.tensors).
        if silence_warnings:
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)

            # Initialize model
        self.create_model()

    def create_model(self):

        # Reset optimizer
        self.optimizer = None

        # Check input consistency
        if self.train_y.shape[0] != self.train_X.shape[0]:
            raise ValueError(
                f"Dim 0 of `train_y` must be equal to the number of training"
                f"samples but is {self.train_y.shape[0]} != {self.train_X.shape[0]}."
            )

        if self.train_y.shape[1] != self.num_tasks:
            raise ValueError(
                f"Dim 1 of `train_y` must be equal to the number of tasks"
                f"but is {self.train_y.shape[1]} != {self.num_tasks}"
            )

        self.likelihood = gpytorch.likelihoods.MultitaskGaussianLikelihood(
            num_tasks=self.num_tasks
        )
        self.model = BatchIndependentMultitaskGPModel(
            self.train_X, self.train_y, self.likelihood, self.num_tasks
        )

    def fit(self, train_X=None, train_y=None):

        # If arguments are passed, delete the previously storred `train_X` and `train_y`
        if (train_X is None) or (train_y is None):
            warnings.warn(
                "Calling `.fit()` with no input argumentss will re-instantiate "
                "and fit the model for the currently stored training data."
            )
        else:
            self.train_X = torch.atleast_2d(torch.tensor(train_X).float())
            self.train_y = torch.atleast_2d(torch.tensor(train_y).float())

        # Create model
        self.create_model()

        # Switch the model to train mode
        self.model.train()
        self.likelihood.train()

        # Define optimizer
        if self.optimizer is None:
            self.optimizer = torch.optim.Adam(
                self.model.parameters(), lr=self.learning_rate
            )  # Includes GaussianLikelihood parameters

        # Define marginal loglikelihood
        self.mll = ExactMarginalLogLikelihood(self.likelihood, self.model)

        # Train
        self.vec_loss = []
        loss_0 = np.inf
        for _i in range(self.training_max_iter):

            self.optimizer.zero_grad()
            output = self.model(self.train_X)
            loss = -self.mll(output, self.train_y)
            loss.backward()
            self.vec_loss.append(loss.item())
            self.optimizer.step()

            # TODO: Will this work for negative losss? CHECK
            loss_ratio = None
            if self.min_loss_rate:
                loss_ratio = (loss_0 - loss.item()) - self.min_loss_rate * loss.item()

            # From https://stackoverflow.com/questions/5290994
            # /remove-and-replace-printed-items
            if self.show_progress:
                print(
                    f"Iter = {_i} / {self.training_max_iter},"
                    f" Loss = {loss.item()}, Loss_ratio = {loss_ratio}",
                    end="\r",
                    flush=True,
                )

            # Get noise value
            self.noise_std = self.get_noise()

            # Check criterion and break if true
            if self.min_loss_rate:
                if loss_ratio < 0.0:
                    break

            # Set previous iter loss to current
            loss_0 = loss.item()

        print(
            f"Iter = {self.training_max_iter},"
            f" Loss = {loss.item()}, Loss_ratio = {loss_ratio}"
        )

    def predict(self, X_pred, return_std=False):

        # Cast input to tensor for compatibility with sampling algorithms
        X_pred = torch.atleast_2d(torch.tensor(X_pred).float())

        # Switch the model to eval mode
        self.model.eval()
        self.likelihood.eval()

        # Make prediction
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            self.prediction = self.likelihood(self.model(X_pred))

        # Get mean, variance and std. dev per model
        self.mean = self.prediction.mean
        self.var = self.prediction.variance
        self.std = self.prediction.variance.sqrt()

        # Get confidence intervals per model
        self.cr_l, self.cr_u = self.prediction.confidence_region()

        if return_std:
            return self.prediction.sample(), self.std
        else:
            return self.prediction.sample()

    def sample_posterior(self, n_samples=1):
        # Switch the model to eval mode
        self.model.eval()
        self.likelihood.eval()

        return self.prediction.n_sample(n_samples)

    def update(self, new_X, new_y):
        full_X = torch.cat([self.train_X, new_X], dim=0)
        full_y = torch.cat([self.train_y, new_y], dim=0)

        self.optimizer = None
        self.fit(full_X, full_y)

    def get_noise(self):
        return self.model.likelihood.noise.sqrt()


class MultiTaskGaussianProcess(Surrogate):
    """
    !!!!!!!!!!!! IN PROGRESS !!!!!!!!!!!!!!!

    Utility class to generate a surrogate composed of multiple correlated
    Gaussian processes:
    https://docs.gpytorch.ai/en/stable/examples/03_Multitask_Exact_GPs/Multitask_GP_Regression.html

    Notes:
        * This model must be initialized with data
        * The `.fit(X, y)` method replaces the current `train_X` and `train_y`
        with its arguments every time it is called.
        * The `.update(X, y)` method will append the new X and y training tensors
        to the existing `train_X`, `train_y` and perform the fitting.
        * Both `.fit()` and `.update()` will re-instantiate a new model. There
        is likely a better solution to this.

    TODO:
    * GpyTorch probably has existing functionality for updating and refitting
    models. This is likely the prefered approach and should replace the current
    approach where the model is redefined at each call to `fit()` or `.update()`.
    * Rewrite to use the existing Gaussian process surrogate class
    * Add type hinting
    * Improve docstrings
    """

    is_probabilistic = True

    def __init__(
        self,
        train_X,
        train_y,
        num_tasks,
        training_max_iter=100,
        learning_rate=0.1,
        min_loss_rate=None,
        optimizer=None,
        mean=None,
        covar=None,
        show_progress=True,
        silence_warnings=False,
    ):

        self.model = None
        self.train_X = train_X
        self.train_y = train_y
        self.num_tasks = num_tasks
        self.training_max_iter = training_max_iter
        self.noise_std = None
        self.likelihood = None
        self.mll = None
        self.learning_rate = learning_rate
        self.min_loss_rate = min_loss_rate
        self.mean_module = mean
        self.covar_module = covar
        self.optimizer = optimizer
        self.show_progress = show_progress
        self.predictions = None

        if self.optimizer is None:
            warnings.warn("No optimizer specified, using default.", UserWarning)

        # Silence torch and numpy warnings (related to converting
        # between np.arrays and torch.tensors).
        if silence_warnings:
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)

            # Initialize model
        self.create_model()

    def create_model(self):

        # Reset optimizer
        self.optimizer = None

        # Check input consistency
        if self.train_y.shape[0] != self.train_X.shape[0]:
            raise ValueError(
                f"Dim 0 of `train_y` must be equal to the number of training"
                f"samples but is {self.train_y.shape[0]} != {self.train_X.shape[0]}."
            )

        if self.train_y.shape[1] != self.num_tasks:
            raise ValueError(
                f"Dim 1 of `train_y` must be equal to the number of tasks"
                f"but is {self.train_y.shape[1]} != {self.num_tasks}"
            )

        self.likelihood = gpytorch.likelihoods.MultitaskGaussianLikelihood(
            num_tasks=self.num_tasks
        )
        self.model = MultitaskGPModel(
            self.train_X, self.train_y, self.likelihood, self.num_tasks
        )

    def fit(self, train_X=None, train_y=None):

        # If arguments are passed, delete the previously storred `train_X` and `train_y`
        if (train_X is None) or (train_y is None):
            warnings.warn(
                "Calling `.fit()` with no input argumentss will re-instantiate "
                "and fit the model for the currently stored training data."
            )
        else:
            self.train_X = torch.atleast_2d(torch.tensor(train_X).float())
            self.train_y = torch.atleast_2d(torch.tensor(train_y).float())

        # Create model
        self.create_model()

        # Switch the model to train mode
        self.model.train()
        self.likelihood.train()

        # Define optimizer
        if self.optimizer is None:
            self.optimizer = torch.optim.Adam(
                self.model.parameters(), lr=self.learning_rate
            )  # Includes GaussianLikelihood parameters

        # Define marginal loglikelihood
        self.mll = ExactMarginalLogLikelihood(self.likelihood, self.model)

        # Train
        self.vec_loss = []
        loss_0 = np.inf
        for _i in range(self.training_max_iter):

            self.optimizer.zero_grad()
            output = self.model(self.train_X)
            loss = -self.mll(output, self.train_y)
            loss.backward()
            self.vec_loss.append(loss.item())
            self.optimizer.step()

            # TODO: Will this work for negative losss? CHECK
            loss_ratio = None
            if self.min_loss_rate:
                loss_ratio = (loss_0 - loss.item()) - self.min_loss_rate * loss.item()

            # From https://stackoverflow.com/questions/5290994
            # /remove-and-replace-printed-items
            if self.show_progress:
                print(
                    f"Iter = {_i} / {self.training_max_iter},"
                    f" Loss = {loss.item()}, Loss_ratio = {loss_ratio}",
                    end="\r",
                    flush=True,
                )

            # Get noise value
            self.noise_std = self.get_noise()

            # Check criterion and break if true
            if self.min_loss_rate:
                if loss_ratio < 0.0:
                    break

            # Set previous iter loss to current
            loss_0 = loss.item()

        print(
            f"Iter = {self.training_max_iter},"
            f" Loss = {loss.item()}, Loss_ratio = {loss_ratio}"
        )

    def predict(self, X_pred, return_std=False):

        # Cast input to tensor for compatibility with sampling algorithms
        X_pred = torch.atleast_2d(torch.tensor(X_pred).float())

        # Switch the model to eval mode
        self.model.eval()
        self.likelihood.eval()

        # Make prediction
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            self.prediction = self.likelihood(self.model(X_pred))

        # Get mean, variance and std. dev per model
        self.mean = self.prediction.mean
        self.var = self.prediction.variance
        self.std = self.prediction.variance.sqrt()

        # Get confidence intervals per model
        self.cr_l, self.cr_u = self.prediction.confidence_region()

        if return_std:
            return self.prediction.sample(), self.std
        else:
            return self.prediction.sample()

    def sample_posterior(self, n_samples=1):
        # Switch the model to eval mode
        self.model.eval()
        self.likelihood.eval()

        return self.prediction.n_sample(n_samples)

    def update(self, new_X, new_y):
        full_X = torch.cat([self.train_X, new_X], dim=0)
        full_y = torch.cat([self.train_y, new_y], dim=0)

        self.optimizer = None
        self.fit(full_X, full_y)

    def get_noise(self):
        return self.model.likelihood.noise.sqrt()


class NN(Surrogate):
    learning_rate_initial = 0.01
    learning_rate_update = 0.001
    is_probabilistic = False

    def __init__(self, **kwargs):
        self.model = None

        self.create_model()

    def create_model(self, input_dim=(2,)):
        inputs = Input(shape=input_dim)
        hidden = Dense(64, activation="relu")(inputs)
        hidden = Dense(32, activation="relu")(hidden)
        hidden = Dense(16, activation="relu")(hidden)
        out = Dense(1)(hidden)
        optimizer = Adam(learning_rate=self.learning_rate_initial)
        self.model = Model(inputs=inputs, outputs=out)
        self.model.compile(optimizer=optimizer, loss="mse")

    def fit(self, X, y, epochs=10):
        self.model.fit(X, y, epochs=epochs, batch_size=32)

    def update(self, X_new, y_new):
        optimizer = Adam(learning_rate=self.learning_rate_update)
        self.model.compile(optimizer=optimizer, loss=root_mean_squared_error)
        self.model.fit(X_new, y_new, epochs=10, batch_size=32, verbose=False)

    def predict(self, X):
        if self.model:
            if len(X.shape) == 1:
                X = np.expand_dims(X, axis=0)
            preds = self.model.predict(X)

            return preds

    def get_model_parameters(self):
        if self.model is not None:
            return self.model.get_weights()

    def get_fmin(self):
        return self.model.predict(self.X).min()


class Prob_NN(Surrogate):
    learning_rate_initial = 0.01
    learning_rate_update = 0.001
    is_probabilistic = True

    def __init__(self, **kwargs):
        self.model = None

        self.create_model()

    def create_model(self):
        inputs = Input(shape=(2,))
        hidden = Dense(20, activation="relu")(inputs)
        hidden = Dense(50, activation="relu")(hidden)
        hidden = Dense(20, activation="relu")(hidden)
        params = Dense(2)(hidden)
        dist = tfp.layers.DistributionLambda(normal_sp)(params)
        optimizer = Adam(learning_rate=self.learning_rate_initial)
        self.model = Model(inputs=inputs, outputs=dist)
        self.model.compile(optimizer=optimizer, loss=NLL)

    def fit(self, X, y, epochs=10):
        self.model.fit(X, y, epochs=epochs, batch_size=32)

    def update(self, X_new, y_new):
        optimizer = Adam(learning_rate=self.learning_rate_update)
        self.model.compile(optimizer=optimizer, loss=NLL)
        self.model.fit(X_new, y_new, epochs=10, batch_size=32)

    def predict(self, X, iterations=50, return_samples=False):
        if self.model:
            preds = np.zeros(shape=(X.shape[0], iterations))

            for i in range(iterations):
                y_ = self.model.predict(X)
                y__ = np.reshape(y_, (X.shape[0]))
                preds[:, i] = y__

            mean = np.mean(preds, axis=1)
            stdv = np.std(preds, axis=1)

            if return_samples:
                mean, stdv, preds

            return mean, stdv

    def get_model_parameters(self):
        if self.model is not None:
            return self.model.get_weights()

    def get_fmin(self):
        return self.model.predict(self.X).min()


class Bayesian_NN(Surrogate):
    learning_rate_initial = 0.01
    learning_rate_update = 0.001
    is_probabilistic = True

    def __init__(self, X, **kwargs):
        self.model = None
        self.is_probabilistic = True

        self.create_model(X)

    def create_model(self, x):
        def kernel_divergence_fn(q, p, _):
            return tfp.distributions.kl_divergence(q, p) / (x.shape[0] * 1.0)

        def bias_divergence_fn(q, p, _):
            return tfp.distributions.kl_divergence(q, p) / (x.shape[0] * 1.0)

        inputs = Input(shape=(2,))

        hidden = tfp.layers.DenseFlipout(
            128,
            bias_posterior_fn=tfp.layers.util.default_mean_field_normal_fn(),
            bias_prior_fn=tfp.layers.default_multivariate_normal_fn,
            kernel_divergence_fn=kernel_divergence_fn,
            bias_divergence_fn=bias_divergence_fn,
            activation="relu",
        )(inputs)
        hidden = tfp.layers.DenseFlipout(
            64,
            bias_posterior_fn=tfp.layers.util.default_mean_field_normal_fn(),
            bias_prior_fn=tfp.layers.default_multivariate_normal_fn,
            kernel_divergence_fn=kernel_divergence_fn,
            bias_divergence_fn=bias_divergence_fn,
            activation="relu",
        )(hidden)
        hidden = tfp.layers.DenseFlipout(
            32,
            bias_posterior_fn=tfp.layers.util.default_mean_field_normal_fn(),
            bias_prior_fn=tfp.layers.default_multivariate_normal_fn,
            kernel_divergence_fn=kernel_divergence_fn,
            bias_divergence_fn=bias_divergence_fn,
            activation="relu",
        )(hidden)
        hidden = tfp.layers.DenseFlipout(
            16,
            bias_posterior_fn=tfp.layers.util.default_mean_field_normal_fn(),
            bias_prior_fn=tfp.layers.default_multivariate_normal_fn,
            kernel_divergence_fn=kernel_divergence_fn,
            bias_divergence_fn=bias_divergence_fn,
            activation="relu",
        )(hidden)
        params = tfp.layers.DenseFlipout(
            2,
            bias_posterior_fn=tfp.layers.util.default_mean_field_normal_fn(),
            bias_prior_fn=tfp.layers.default_multivariate_normal_fn,
            kernel_divergence_fn=kernel_divergence_fn,
            bias_divergence_fn=bias_divergence_fn,
        )(hidden)
        dist = tfp.layers.DistributionLambda(normal_sp)(params)

        self.model = Model(inputs=inputs, outputs=dist)
        self.model.compile(Adam(learning_rate=self.learning_rate_initial), loss=NLL)

    def fit(self, X, y, epochs=25, verbose=0):
        self.X = X
        self.y = y
        self.model.fit(X, y, epochs=epochs, batch_size=32, verbose=verbose)

    def update(self, X_new, y_new, epochs=25, verbose=0):
        if self.model is None:
            self._create_model()
        else:
            self.model.compile(Adam(learning_rate=self.learning_rate_update), loss=NLL)
            self.model.fit(X_new, y_new, epochs=epochs, batch_size=32, verbose=verbose)

    def predict(self, X, iterations=50, return_samples=False):
        if self.model:
            preds = np.zeros(shape=(X.shape[0], iterations))

            for i in range(iterations):
                y_ = self.model.predict(X)
                y__ = np.reshape(y_, (X.shape[0]))
                preds[:, i] = y__

            mean = np.mean(preds, axis=1)
            stdv = np.std(preds, axis=1)

            if return_samples:
                mean, stdv, preds

            return mean, stdv

    def get_model_parameters(self):
        if self.model is not None:
            return self.model.get_weights()

    def get_fmin(self):
        return self.model.predict(self.X).min()
