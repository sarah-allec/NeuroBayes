from typing import Dict, Optional, Type, Callable, Tuple
import jax.numpy as jnp
import flax

import numpyro
import numpyro.distributions as dist
from numpyro.contrib.module import random_flax_module

from .dkl import DKL
from ..utils.priors import GPPriors
from ..flax_nets import FlaxMLP, FlaxConvNet, split_mlp, split_convnet
from ..flax_nets import DeterministicNN

kernel_fn_type = Callable[[jnp.ndarray, jnp.ndarray, Dict[str, jnp.ndarray], jnp.ndarray],  jnp.ndarray]


class PartialDKL(DKL):
    """
    Partially stochastic DKL
    """

     # Dictionary mapping network types to their corresponding splitter functions
    SPLITTERS = {
        FlaxMLP: split_mlp,
        FlaxConvNet: split_convnet,
        # More network types and their splitters TBA
    }

    def __init__(self,
                 latent_dim: int,
                 base_kernel: kernel_fn_type,
                 deterministic_nn: Type[flax.linen.Module],
                 deterministic_weights: Optional[Dict[str, jnp.ndarray]] = None,
                 num_stochastic_layers: int = 1,
                 priors: Optional[GPPriors] = None,
                 jitter: float = 1e-6,
                 ) -> None:
        super(PartialDKL, self).__init__(latent_dim, base_kernel, priors, jitter)

        self.nn_type = type(deterministic_nn)
        if self.nn_type not in self.SPLITTERS:
            raise ValueError(f"Unsupported network type: {self.nn_type}")
        self.splitter = self.SPLITTERS[self.nn_type]

        if deterministic_weights:
            (self.truncated_nn, self.truncated_params,
             self.nn) = self.splitter(
                 deterministic_nn, deterministic_weights,
                 num_stochastic_layers, latent_dim)[:-1]
        else:
            self.untrained_deterministic_nn = deterministic_nn
            self.num_stochastic_layers = num_stochastic_layers
            self.latent_dim = latent_dim

    def model(self, X: jnp.ndarray, y: jnp.ndarray = None, **kwargs) -> None:
        """DKL probabilistic model"""
        # Get inputs through a deterministic NN part
        X = self.truncated_nn.apply({'params': self.truncated_params}, X)
        # Fully stochastic NN part
        bnn = random_flax_module(
            "nn", self.nn, input_shape=(1, X.shape[-1]),
            prior=(lambda name, shape: dist.Cauchy() if name == "bias" else dist.Normal()))
        # Latent encoding
        z = bnn(X)
        # GP Part
        f_loc = jnp.zeros(X.shape[0])
        # Sample kernel parameters
        kernel_params = self.sample_kernel_params(kernel_dim=z.shape[-1])
        # Sample observational noise variance
        noise = self.sample_noise()
        # Compute kernel
        k = self.kernel(z, z, kernel_params, noise, self.jitter)
        # Sample y according to the standard Gaussian process formula
        numpyro.sample(
            "y",
            dist.MultivariateNormal(loc=f_loc, covariance_matrix=k),
            obs=y,
        )

    def fit(self, X: jnp.ndarray, y: jnp.ndarray,
            num_warmup: int = 2000, num_samples: int = 2000,
            num_chains: int = 1, chain_method: str = 'sequential',
            sgd_epochs: Optional[int] = None, sgd_lr: float = 0.01,
            sgd_batch_size: Optional[int] = None, sgd_wa_epochs: Optional[int] = 10,
            map_sigma: float = 1.0, progress_bar: bool = True, print_summary: bool = True,
            device: str = None, rng_key: Optional[jnp.array] = None,
            extra_fields: Optional[Tuple[str]] = ()) -> None:
        """
        Run HMC to infer parameters of the DKL

        Args:
            X: 2D feature vector
            y: 1D target vector
            num_warmup: number of HMC warmup states
            num_samples: number of HMC samples
            num_chains: number of HMC chains
            chain_method: 'sequential', 'parallel' or 'vectorized'
            sgd_epochs:
                number of SGD training epochs for deterministic NN
                (if trained weights are not provided at the initialization stage)
            sgd_lr: SGD learning rate (if trained weights are not provided at the initialization stage)
            sgd_batch_size:
                Batch size for SGD training (if trained weights are not provided at the initialization stage).
                Defaults to None, meaning that an entire dataset is passed through an NN.
            sgd_wa_epochs: Number of epochs for stochastic weight averaging at the end of SGD training trajectory (defautls to 10)
            map_sigma: sigma in gaussian prior for regularized SGD training
            progress_bar: show progress bar
            print_summary: Print MCMC summary
            device:
                The device (e.g. "cpu" or "gpu") perform computation on ('cpu', 'gpu'). If None, computation
                is performed on the JAX default device.
            rng_key: random number generator key
            extra_fields:
                Extra fields (e.g. 'accept_prob') to collect during the HMC run.
                The extra fields are accessible from model.mcmc.get_extra_fields() after model training.
            
        """
        if hasattr(self, "untrained_deterministic_nn"):
            print("Training deterministic NN...")
            X = self.set_data(X)
            det_nn = DeterministicNN(
                self.untrained_deterministic_nn,
                input_shape = X.shape[1:] if X.ndim > 2 else (X.shape[-1],), # different input dims for ConvNet and MLP
                learning_rate=sgd_lr, swa_epochs=sgd_wa_epochs, sigma=map_sigma)
            det_nn.train(X, y, 500 if sgd_epochs is None else sgd_epochs, sgd_batch_size)
            (self.truncated_nn, self.truncated_params,
            self.nn) = self.splitter(
                det_nn.model, det_nn.state.params,
                self.num_stochastic_layers, self.latent_dim)[:-1]
            print("Training partially Bayesian DKL")
        super().fit(X, y, num_warmup, num_samples, num_chains, chain_method, progress_bar, print_summary, device, rng_key, extra_fields)

    def compute_gp_posterior(self,
                             X_new: jnp.ndarray,
                             X_train: jnp.ndarray,
                             y_train: jnp.ndarray,
                             params: Dict[str, jnp.ndarray],
                             noiseless: bool = True
                             ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        
        # Pass inputs through a deterministc part of NN
        X_new = self.truncated_nn.apply({'params': self.truncated_params}, X_new)
        X_train = self.truncated_nn.apply({'params': self.truncated_params}, X_train)

        # Proceed with the original DKL computations
        return super().compute_gp_posterior(X_new, X_train, y_train, params, noiseless)
