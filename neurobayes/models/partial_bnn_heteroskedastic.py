from typing import List, Optional, Type, Dict, Tuple, Union
import jax.numpy as jnp
import numpyro
import numpyro.distributions as dist
from numpyro.contrib.module import random_flax_module
import flax

from .bnn_heteroskedastic import HeteroskedasticBNN
from ..flax_nets import DeterministicNN
from ..flax_nets import FlaxMLP2Head, FlaxConvNet2Head
from ..flax_nets import extract_mlp2head_configs, MLPLayerModule



class HeteroskedasticPartialBNN(HeteroskedasticBNN):
    """
    Heteroskedastic Partially Bayesian Neural Network

    Args:
        deterministic_nn:
            Neural network architecture (MLP, ConvNet, or other supported types)
        deterministic_weights:
            Pre-trained deterministic weights, If not provided,
            the deterministic_nn will be trained from scratch when running .fit() method
        num_probabilistic_layers
            Number of layers at the end of deterministic_nn to be treated as fully stochastic ('Bayesian')
        probabilistic_layer_names:
            Names of neural network modules to be treated probabilistically
    """

    def __init__(self,
                 deterministic_nn: Union[Type[FlaxMLP2Head], Type[FlaxConvNet2Head]],
                 deterministic_weights: Optional[Dict[str, jnp.ndarray]] = None,
                 num_probabilistic_layers: int = None,
                 probabilistic_layer_names: List[str] = None,
                 ) -> None:
        super().__init__(None)

        self.deterministic_nn = deterministic_nn
        self.deterministic_weights = deterministic_weights

        self.layer_configs = extract_mlp2head_configs(
            deterministic_nn, probabilistic_layer_names, num_probabilistic_layers)

    def model(self,
              X: jnp.ndarray,
              y: jnp.ndarray = None,
              pretrained_priors: Dict = None,
              priors_sigma: float = 1.0,
              **kwargs) -> None:
        """Heteroskedastic (partial) BNN probabilistic model"""

        net = self.deterministic_nn
        pretrained_priors = {}
        for module_dict in self.deterministic_weights.values():
            pretrained_priors.update(module_dict)
            
        def prior(name, shape):
            param_path = name.split('.')
            layer_name = param_path[0]
            param_type = param_path[-1]  # kernel or bias
            return dist.Normal(pretrained_priors[layer_name][param_type], priors_sigma)

        current_input = X
        
        # Process shared layers
        for idx, config in enumerate(self.layer_configs[:-2]):  # All but the two head layers
            layer_name = config["layer_name"]
            layer = MLPLayerModule(
                features=config['features'],
                activation=config['activation'],
                layer_name=layer_name
            )
            
            if config['is_probabilistic']:
                net = random_flax_module(
                    layer_name, layer, 
                    input_shape=(1, current_input.shape[-1]),
                    prior=prior
                )
                current_input = net(current_input)
            else:
                params = {
                    "params": {
                        layer_name: {
                            "kernel": pretrained_priors[layer_name]["kernel"],
                            "bias": pretrained_priors[layer_name]["bias"]
                        }
                    }
                }
                current_input = layer.apply(params, current_input)

        # Process head layers
        shared_output = current_input
        
        # Mean head
        mean_config = self.layer_configs[-2]
        layer_name = mean_config["layer_name"]
        mean_layer = MLPLayerModule(
            features=mean_config['features'],
            activation=mean_config['activation'],
            layer_name=layer_name
        )
        
        if mean_config['is_probabilistic']:
            net = random_flax_module(
                layer_name, mean_layer,
                input_shape=(1, shared_output.shape[-1]),
                prior=prior
            )
            mean = net(shared_output)
        else:
            params = {
                "params": {
                    layer_name: {
                        "kernel": pretrained_priors[layer_name]["kernel"],
                        "bias": pretrained_priors[layer_name]["bias"]
                    }
                }
            }
            mean = mean_layer.apply(params, shared_output)
        
        # Variance head
        var_config = self.layer_configs[-1]
        layer_name = var_config["layer_name"]
        var_layer = MLPLayerModule(
            features=var_config['features'],
            activation=var_config['activation'],
            layer_name=layer_name
        )
        
        if var_config['is_probabilistic']:
            net = random_flax_module(
                layer_name, var_layer,
                input_shape=(1, shared_output.shape[-1]),
                prior=prior
            )
            variance = net(shared_output)
        else:
            params = {
                "params": {
                    "VarianceHead": {
                        "kernel": pretrained_priors[layer_name]["kernel"],
                        "bias": pretrained_priors[layer_name]["bias"]
                    }
                }
            }
            variance = var_layer.apply(params, shared_output)

        # Register values with numpyro
        mu = numpyro.deterministic("mu", mean)
        sig = numpyro.deterministic("sig", variance)

        # Score against the observed data points
        numpyro.sample("y", dist.Normal(mu, sig), obs=y)

    def fit(self, X: jnp.ndarray, y: jnp.ndarray,
            num_warmup: int = 2000, num_samples: int = 2000,
            num_chains: int = 1, chain_method: str = 'sequential',
            sgd_epochs: Optional[int] = None, sgd_lr: Optional[float] = 0.01,
            sgd_batch_size: Optional[int] = None, sgd_wa_epochs: Optional[int] = 10,
            map_sigma: float = 1.0, priors_from_map: bool = False, priors_sigma: float = 1.0,
            progress_bar: bool = True, device: str = None, rng_key: Optional[jnp.array] = None,
            extra_fields: Optional[Tuple[str]] = ()
            ) -> None:
        """
        Run HMC to infer parameters of the heteroskedastic BNN

        Args:
            X: 2D feature vector
            y: 1D target vector
            num_warmup: number of HMC warmup states
            num_samples: number of HMC samples
            num_chains: number of HMC chains
            chain_method: choose between 'sequential', 'vectorized', and 'parallel'
            sgd_swa_epochs:
                number of SGD training epochs for deterministic NN
                (if trained weights are not provided at the initialization stage)
            sgd_lr: SGD learning rate (if trained weights are not provided at the initialization stage)
            sgd_batch_size:
                Batch size for SGD training (if trained weights are not provided at the initialization stage).
                Defaults to None, meaning that an entire dataset is passed through an NN.
            sgd_wa_epochs: Number of epochs for stochastic weight averaging at the end of SGD training trajectory (defautls to 10)
            map_sigma: sigma in gaussian prior for regularized SGD training
            priors_sigma: Standard deviation for default or pretrained priors (defaults to 1.0)
            progress_bar: show progress bar
            device:
                The device (e.g. "cpu" or "gpu") perform computation on ('cpu', 'gpu'). If None, computation
                is performed on the JAX default device.
            rng_key: random number generator key
            extra_fields:
                Extra fields (e.g. 'accept_prob') to collect during the HMC run.
                The extra fields are accessible from model.mcmc.get_extra_fields() after model training.
        """
        if not self.deterministic_weights:
            print("Training deterministic NN...")
            X, y = self.set_data(X, y)
            det_nn = DeterministicNN(
                self.deterministic_nn,
                input_shape = X.shape[1:] if X.ndim > 2 else (X.shape[-1],), # different input dims for ConvNet and MLP 
                loss='heteroskedastic', learning_rate=sgd_lr,
                swa_epochs=sgd_wa_epochs, sigma=map_sigma)
            det_nn.train(X, y, 500 if sgd_epochs is None else sgd_epochs, sgd_batch_size)
            self.deterministic_weights = det_nn.state.params
            print("Training partially Bayesian NN")
        super().fit(X, y, num_warmup, num_samples, num_chains, chain_method,
                    priors_sigma, progress_bar, device, rng_key, extra_fields)

