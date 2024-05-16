from typing import Dict, Tuple, Optional, Union, List
import jax
import jax.random as jra
import jax.numpy as jnp
from jax import vmap
import numpyro
import numpyro.distributions as dist

from numpyro.infer import MCMC, NUTS, init_to_median

from .nn import get_mlp
from .priors import get_mlp_prior
from .utils import put_on_device, split_dict


class BNN:
    """
    A Fully Bayesian Neural Network.
    This approach employs a probabilistic treatment of all neural network weights,
    treating them as random variables with specified prior distributions
    and utilizing advanced Markov Chain Monte Carlo techniques to sample directly
    from the posterior distribution, allowing to account for all plausible weight configurations.
    This approach enables the network to make probabilistic predictions,
    not just single-point estimates but entire distributions of possible outcomes,
    quantifying the inherent uncertainty.
    """

    def __init__(self,
                 input_dim: int,
                 output_dim: int,
                 hidden_dim: List[int] = None,
                 activation: str = 'tanh',
                 noise_prior: Optional[dist.Distribution] = None
                 ) -> None:
        if noise_prior is None:
            noise_prior = dist.HalfNormal(1.0)
        hdim = hidden_dim if hidden_dim is not None else [32, 16, 8]
        self.nn = get_mlp(hdim, activation)
        self.nn_prior = get_mlp_prior(input_dim, output_dim, hdim)
        self.noise_prior = noise_prior

    def model(self, X: jnp.ndarray, y: jnp.ndarray = None, **kwargs) -> None:
        """BNN probabilistic model"""

        # Sample NN parameters
        nn_params = self.nn_prior()
        # Pass inputs through a NN with the sampled parameters
        mu = self.nn(X, nn_params)

        # Sample noise
        sig = self.sample_noise()

        # Score against the observed data points
        numpyro.sample("y", dist.Normal(mu, sig), obs=y)

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
        return numpyro.sample("sig", self.noise_prior)

    def sample_single_posterior_predictive(self, rng_key, X_new, params, n_draws):
        sigma = params["sig"]
        loc = self.nn(X_new, params)
        sample = dist.Normal(loc, sigma).sample(rng_key, (n_draws,)).mean(0)
        return loc, sample

    def predict(self,
                X_new: jnp.ndarray,
                n_draws: int = 1,
                device: Optional[str] = None,
                rng_key: Optional[jnp.ndarray] = None
                ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Predict the mean and variance of the target values for new inputs.

        Args:
            X_new:
                New input data for predictions.
            n_draws:
                Number of draws to sample from the posterior predictive distribution.
            device:
                The device (e.g. "cpu" or "gpu") perform computation on ('cpu', 'gpu'). If None, computation
                is performed on the JAX default device.
            rng_key:
                Random number generator key for JAX operations.

        Returns:
            Tuple containing the means and samples from the posterior predictive distribution.
        """
        mean, f_samples = self._vmap_predict(X_new, None, n_draws, rng_key, device)
        return mean, f_samples.var(0)

    def predict_in_batches(self, X_new: jnp.ndarray,
                           batch_size: int = 100,
                           n_draws: int = 1,
                           device: Optional[str] = None,
                           rng_key: Optional[jnp.ndarray] = None
                           ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Make prediction in batches (to avoid memory overflow)
        at X_new points a trained BNN model
        """
        samples = self.get_samples(chain_dim=False)
        mean_chunks, f_samples_chunks = [], []
        for batch in split_dict(samples, batch_size):
            mean_i, f_samples_i = self._vmap_predict(X_new, batch, n_draws, rng_key, device)
            mean_i = jax.device_put(mean_i, jax.devices("cpu")[0])
            f_samples_i = jax.device_put(f_samples_i, jax.devices("cpu")[0])
            mean_chunks.append(mean_i[None])
            f_samples_chunks.append(f_samples_i)
        mean_chunks = jnp.concatenate(mean_chunks, axis=0)
        f_samples_chunks = jnp.concatenate(f_samples_chunks)
        
        return mean_chunks.mean(0), f_samples_chunks.var(0)

    def sample_from_posterior(self,
                              X_new: jnp.ndarray,
                              n_draws: int = 1,
                              device: Optional[str] = None,
                              rng_key: Optional[jnp.ndarray] = None
                              ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Predict the mean and variance of the target values for new inputs
        """
        _, f_samples = self._vmap_predict(X_new, None, n_draws, rng_key, device)
        return f_samples

    def _vmap_predict(self,
                      X_new: jnp.ndarray,
                      samples: Dict[str, jnp.ndarray] = None,
                      n_draws: int = 1,
                      rng_key: Optional[jnp.ndarray] = None,
                      device: Optional[str] = None
                      ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Helper method to vectorize predictions over posterior samples
        """
        X_new = self.set_data(X_new)
        if rng_key is None:
            rng_key = jra.PRNGKey(0)
        if samples is None:
            samples = self.get_samples(chain_dim=False)
        X_new, samples = put_on_device(device, X_new, samples)
        num_samples = len(next(iter(samples.values())))
        vmap_args = (jra.split(rng_key, num_samples), samples)

        predictive = lambda p1, p2: self.sample_single_posterior_predictive(p1, X_new, p2, n_draws)
        loc, f_samples = vmap(predictive)(*vmap_args)

        return loc.mean(0), f_samples

    def set_data(self, X: jnp.ndarray, y: Optional[jnp.ndarray] = None
                 ) -> Union[Tuple[jnp.ndarray], jnp.ndarray]:
        X = X if X.ndim > 1 else X[:, None]
        if y is not None:
            y = y[:, None] if y.ndim < 2 else y
            return X, y
        return X
