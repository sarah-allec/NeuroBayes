# NeuroBayes

## What is it for

Active learning optimizes the exploration of large parameter spaces by strategically selecting experiments or simulations, reducing resource use and accelerating discovery. A crucial element is a probabilistic surrogate model, typically a Gaussian Process (GP), which approximates the relationship between control parameters and a target property. However, GPs struggle with systems featuring discontinuities and non-stationarities, common in physical science problems. Fully Bayesian Neural Networks (FBNNs) offer a promising alternative by treating all network weights probabilistically and using advanced Markov Chain Monte Carlo techniques for for direct sampling from the posterior distribution. This approach provides reliable predictive distributions, crucial for decision-making under uncertainty. Although FBNNs are traditionally seen as computationally expensive for big data, many physical science problems involve small data sets, making FBNNs feasible. For more complex parameter spaces, Partially Bayesian Neural Networks (PBNNs) can be used, where only some neurons are Bayesian. This repository enables the use of FBNNs and PBNNs with the No-U-Turn Sampler in active and transfer learning tasks for small and intermediate data volumes, demonstrating their potential in physical science applications.

## How to use
### Fully Bayesian Neural Nets
Initialize a simulator
```python3
import neurobayes as nb

(x_start, x_stop), fn = genfunc.nonstationary2()

# Generate ground truth data
X_domain = np.linspace(x_start, x_stop, 500)
y_true = fn(X_domain)

# Create a measurement function
measure = lambda x: fn(x) + np.random.normal(0, 0.02, size=len(x))

# Generate initial dataset
X_measured = np.random.uniform(x_start, x_stop, 50)
y_measured = measure(X_measured)
```

Run a single shot Gaussian process
```python3
# Initialize model
model = GP(input_dim=1, kernel=nb.kernels.MaternKernel)
# Train model
model.fit(X_measured, y_measured, num_warmup=1000, num_samples=1000)
# Make a prediction on full domain
posterior_mean, posterior_var = model.predict(X_domain)
```

Run a single shot Bayesian neural network
```python3
# Initialize model
model = BNN(input_dim=1, output_dim=1)
# Train model
model.fit(X_measured, y_measured, num_warmup=1000, num_samples=1000)
# Make a prediction on full domain
posterior_mean, posterior_var = model.predict(X_domain)
```

Run active learning with Bayesian neural network
```python3
for step in range(exploration_steps):
    # Intitalize and train model
    model = BNN(1, 1)
    model.fit(X_measured, y_measured, num_warmup=1000, num_samples=1000)
    # Make a prediction on unmeasured points or the full domain
    posterior_mean, posterior_var = model.predict(X_domain)
    # Select next point to evaluate
    next_point_idx = posterior_var.argmax(0)
    X_next = X_domain[next_point_idx]
    # Evaluate function in this point
    y_next = measure(X_next)
    # Update training and test set
    X_measured = np.append(X_measured, X_next[None])
    y_measured = np.append(y_measured, y_next)
```
See full active learning example [here](https://github.com/ziatdinovmax/NeuroBayes/blob/main/example1d.ipynb).
    
### Partially Bayesian Neural Nets
PBNNs follow a similar approach, with the key difference being that a deterministic model must first be defined, along with the specification of stochastic gradient descent parameters. By default, PBNNs are trained using Maximum A Posteriori approximation, with stochastic weight averaging applied at the end of each training trajectory. This allows PBNNs to balance computational efficiency with uncertainty quantification by only treating a subset of neurons probabilistically.
```python3
sgd_epochs = 2000
sgd_lr = 5e-3
num_stochastic_layers = 1

# Initialize a determinsitc neural net
net = FlaxMLP(hidden_dims=[32, 16, 8, 8], output_dim=1)

# Run active learning
for step in range(exploration_steps):
    print('step {}'.format(step))
    # Intitalize and train model
    model = PartialBNN(net, input_dim=8, num_stochastic_layers=num_stochastic_layers)
    model.fit(X_measured, y_measured, sgd_epochs=sgd_epochs, sgd_lr=sgd_lr, sgd_batch_size=16, num_warmup=1000, num_samples=1000)
    # Make a prediction on unmeasured points or the full domain
    posterior_mean, posterior_var = model.predict(X_domain)
    # Select next point to evaluate
    next_point_idx = posterior_var.argmax(0)
    X_next = X_domain[next_point_idx]
    # Evaluate function in this point
    y_next = measure(X_next)
    # Update training and test set
    X_measured = np.append(X_measured, X_next[None])
    y_measured = np.append(y_measured, y_next)
```


