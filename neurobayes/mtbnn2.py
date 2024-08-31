from typing import Dict, Tuple, Optional, Union, List, Sequence
import jax.random as jra
import jax.numpy as jnp

import numpyro
import numpyro.distributions as dist
from numpyro.infer import MCMC, NUTS, init_to_median, Predictive
from numpyro.contrib.module import random_flax_module

from .nn import FlaxMultiTaskMLP2
from .utils import put_on_device, split_dict


class MultitaskBNN2:

    def __init__(self,
                 input_dim: int,
                 output_dims: Sequence[int],
                 num_tasks: int,
                 embedding_dim: int,
                 hidden_dim: List[int] = None,
                 activation: str = 'tanh',
                 noise_prior: Optional[dist.Distribution] = None
                 ) -> None:
        if noise_prior is None:
            noise_prior = dist.HalfNormal(jnp.ones(num_tasks))
        self.hdim = hidden_dim if hidden_dim is not None else [32, 16, 8]
        self.nn = None
        self.input_dim = input_dim
        self.output_dims = output_dims
        self.activation = activation
        self.embedding_dim = embedding_dim
        self.num_tasks = num_tasks
        self.noise_prior = noise_prior

    def model(self, X: jnp.ndarray, y: jnp.ndarray = None, **kwargs) -> None:
        """Multi-task BNN model"""

        tasks = X[:, -1].astype(jnp.int32)
        #X = X[:, :-1]

        net = random_flax_module(
            "nn", self.nn, input_shape=(len(X), self.input_dim + 1),
            prior=(lambda name, shape: dist.Cauchy() if name == "bias" else dist.Normal()))

        # Pass inputs through a NN with the sampled parameters
        mu = numpyro.deterministic("mu", net(X))

        # Sample noise
        sig = self.sample_noise()
        sigma_task = sig[tasks]

        # Score against the observed data points
        numpyro.sample("y", dist.Normal(mu, sigma_task[:, None]), obs=y)

    def fit(self, X: jnp.ndarray, y: jnp.ndarray,
            num_warmup: int = 2000, num_samples: int = 2000,
            num_chains: int = 1, chain_method: str = 'sequential',
            progress_bar: bool = True, device: str = None,
            rng_key: Optional[jnp.array] = None,
            ) -> None:
        """
        Run HMC to infer parameters of the BNN

        Args:
            X: 2D feature vector
            y: 1D target vector
            num_warmup: number of HMC warmup states
            num_samples: number of HMC samples
            num_chains: number of HMC chains
            chain_method: 'sequential', 'parallel' or 'vectorized'
            progress_bar: show progress bar
            device:
                The device (e.g. "cpu" or "gpu") perform computation on ('cpu', 'gpu'). If None, computation
                is performed on the JAX default device.
            rng_key: random number generator key
        """
        task_structure = compute_task_sizes(X[:, -1])
        self.nn = FlaxMultiTaskMLP2(
            self.hdim, self.output_dims, task_structure, self.num_tasks, self.embedding_dim, self.activation)

        key = rng_key if rng_key is not None else jra.PRNGKey(0)
        X, y = self.set_data(X, y)
        X, y = put_on_device(device, X, y)
        init_strategy = init_to_median(num_samples=10)
        kernel = NUTS(self.model, init_strategy=init_strategy)
        self.mcmc = MCMC(
            kernel,
            num_warmup=num_warmup,
            num_samples=num_samples,
            num_chains=num_chains,
            chain_method=chain_method,
            progress_bar=progress_bar,
            jit_model_args=False
        )
        self.mcmc.run(key, X, y)

    def get_samples(self, chain_dim: bool = False) -> Dict[str, jnp.ndarray]:
        """Get posterior samples (after running the MCMC chains)"""
        return self.mcmc.get_samples(group_by_chain=chain_dim)

    def sample_noise(self) -> jnp.ndarray:
        """
        Sample observational noise variance
        """
        return numpyro.sample("sig", self.noise_prior.to_event(1))

    def predict(self,
                X_new: jnp.ndarray,
                samples: Optional[Dict[str, jnp.ndarray]] = None,
                device: Optional[str] = None,
                rng_key: Optional[jnp.ndarray] = None
                ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Predict the mean and variance of the target values for new inputs.

        Args:
            X_new:
                New input data for predictions.
            samples:
                Dictionary of posterior samples with inferred model parameters (weights and biases)
            device:
                The device (e.g. "cpu" or "gpu") perform computation on ('cpu', 'gpu'). If None, computation
                is performed on the JAX default device.
            rng_key:
                Random number generator key for JAX operations.

        Returns:
            Tuple containing the means and samples from the posterior predictive distribution.
        """
        X_new = self.set_data(X_new)

        task_structure = compute_task_sizes(X_new[:, -1])
        self.nn.task_sizes = task_structure

        if rng_key is None:
            rng_key = jra.PRNGKey(0)
        if samples is None:
            samples = self.get_samples(chain_dim=False)
        X_new, samples = put_on_device(device, X_new, samples)

        predictions = self.sample_from_posterior(
            rng_key, X_new, samples, return_sites=["mu", "y"])
        posterior_mean = predictions["mu"].mean(0)
        posterior_var = predictions["y"].var(0)
        return posterior_mean, posterior_var

    def sample_from_posterior(self,
                              rng_key: jnp.ndarray,
                              X_new: jnp.ndarray,
                              samples: Dict[str, jnp.ndarray],
                              return_sites: Optional[List[str]] = None
                              ) -> jnp.ndarray:
   
        predictive = Predictive(
            self.model, samples,
            return_sites=return_sites
        )
        return predictive(rng_key, X_new)

    def set_data(self, X: jnp.ndarray, y: Optional[jnp.ndarray] = None
                 ) -> Union[Tuple[jnp.ndarray], jnp.ndarray]:
        X = X if X.ndim > 1 else X[:, None]
        if y is not None:
            y = y[:, None] if y.ndim < 2 else y
            return X, y
        return X


def compute_task_sizes(indices: jnp.ndarray) -> Dict[str, int]:
    """
    Compute task sizes from an array of indices, using string keys.
    
    Args:
    indices (jnp.ndarray): 1D array of task indices, e.g., [0, 0, 0, 1, 1, 3, 3, 3, 3, 3, 3, 5, 5]
    
    Returns:
    Dict[str, int]: Dictionary mapping string task indices to their sizes, e.g., {'0': 3, '1': 2, '3': 6, '5': 2}
    """
    if indices.ndim != 1:
        raise ValueError("Input must be a 1D array")
    
    unique_indices, counts = jnp.unique(indices, return_counts=True)
    return {str(int(idx)): int(count) for idx, count in zip(unique_indices, counts)}