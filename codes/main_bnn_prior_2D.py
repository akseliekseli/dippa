import pickle
import argparse
import yaml


import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import torch
import torch.nn as nn
import arviz as az

from scipy.interpolate import CubicSpline


import pyro
import pyro.distributions as dist
from pyro.nn import PyroModule, PyroSample
from pyro.infer import SVI, Trace_ELBO
from pyro.infer.autoguide import AutoDiagonalNormal
from pyro.optim import Adam
from tqdm.auto import trange

from models import Deconvolution_2D

#plt.rcParams["font.family"] = "Deja Vu"
#plt.rcParams['font.size'] = 32

n = 50

class BNNGuide(PyroModule):
    def __init__(self, model):
        super().__init__()

        self.n_layers = model.n_layers
        self.layers = PyroModule[torch.nn.ModuleList]([])
        self.model = model
        for ii, layer in enumerate(model.layers):
            # Mean and std of weights
            self.layers.append(PyroModule[nn.Linear](layer.in_features, layer.out_features))
            
            if isinstance(layer.weight, PyroSample):
                weight_loc = pyro.param(f"weight_loc_{ii}", torch.randn_like(layer.weight.mean))
                weight_scale = pyro.param(f"weight_scale_{ii}", torch.randn_like(layer.weight.mean).abs())
                
                self.layers[ii].weight = PyroSample(dist.Normal(weight_loc, weight_scale).to_event(2))
            
            if isinstance(layer.bias, PyroSample):
                bias_loc = pyro.param(f"bias_loc_{ii}", torch.randn_like(layer.bias.mean))
                bias_scale = pyro.param(f"bias_scale_{ii}", torch.randn_like(layer.bias.mean).abs())
                
                self.layers[ii].bias = PyroSample(dist.Normal(bias_loc, bias_scale).to_event(2))
        
        self.sigma = pyro.param("sigma", torch.tensor(0.01), constraint=dist.constraints.positive)


    def forward(self, t, A, y=None):
        # Sample sigma
        pyro.sample("sigma", dist.Delta(torch.tensor(0.01)))

        # Passing the input through the network
        for ii in range(self.n_layers):
            t = self.layers[ii](t)
            if self.model.activations[ii] == 'tanh':
                t = torch.tanh(t)
            elif self.model.activations[ii] == 'relu':
                t = torch.relu(t)
        
        return t.flatten()



class BNN(PyroModule):

    def __init__(self, n_in, n_out, layers, conv):
        super().__init__()
        self.conv = conv
        self.n_layers = len(layers)
        print(n_out)
        self.layers = PyroModule[torch.nn.ModuleList]([])
        self.activations = []
        for ii, layer in enumerate(layers):
            # Creating and appending torch layer
            if ii == 0:
                self.layers.append(PyroModule[nn.Linear](n_in, layers[layer]['layer_size']))
            elif ii == self.n_layers-1:
                input = self.layers[-1].out_features
                self.layers.append(PyroModule[nn.Linear](input, n_out))
                print(f'OUT: {self.layers[-1].out_features}')
            else:
                input = self.layers[-1].out_features
                self.layers.append(PyroModule[nn.Linear](input, layers[layer]['layer_size']))


            # Scaling the weights, for Gaussian n^(-1/2) and for Cauchy n^-1
            if ii != 0:
                if layers[layer]['type'] == 'gaussian':
                    weight_scale = float(1 / np.sqrt(self.layers[ii].in_features))
                elif layers[layer]['type'] == 'cauchy':
                    weight_scale = float(1 / self.layers[ii].in_features)
            else:
                weight_scale = 1.0

            weight = float(layers[layer]['weight'] * weight_scale)
            bias = float(layers[layer]['bias'])

            if layers[layer]['type'] == 'cauchy':
                if ii == 0:  # First layer
                    self.layers[ii].weight = PyroSample(dist.Cauchy(0., torch.tensor(weight)).expand([self.layers[ii].out_features, n_in]).to_event(2))
                elif ii == self.n_layers-1:
                    self.layers[ii].weight = PyroSample(dist.Cauchy(0., torch.tensor(weight)).expand([1, self.layers[ii].in_features]).to_event(2))
                else:
                    self.layers[ii].weight = PyroSample(dist.Cauchy(0., torch.tensor(weight)).expand([self.layers[ii].out_features, self.layers[ii].in_features]).to_event(2))

                if ii == self.n_layers - 1:  # Special case for the last layer
                    self.layers[ii].bias = PyroSample(dist.Cauchy(0., torch.tensor(bias)).expand([1, 1]).to_event(2))
                    #pass
                else:
                    self.layers[ii].bias = PyroSample(dist.Cauchy(0., torch.tensor(bias)).expand([1, self.layers[ii].out_features]).to_event(2))
                
            elif layers[layer]['type'] == 'gaussian':
                if ii == 0:  # First layer
                    self.layers[ii].weight = PyroSample(dist.Normal(0., torch.tensor(weight)).expand([self.layers[ii].out_features, n_in]).to_event(2))
                elif ii == self.n_layers-1:
                    self.layers[ii].weight = PyroSample(dist.Normal(0., torch.tensor(weight)).expand([1, self.layers[ii].in_features]).to_event(2))
                else:
                    self.layers[ii].weight = PyroSample(dist.Normal(0., torch.tensor(weight)).expand([self.layers[ii].out_features, self.layers[ii].in_features]).to_event(2))

                if ii == self.n_layers - 1:  # Special case for the last layer
                    self.layers[ii].bias = PyroSample(dist.Normal(0., torch.tensor(bias)).expand([1, 1]).to_event(2))
                    #pass
                else:
                    self.layers[ii].bias = PyroSample(dist.Normal(0., torch.tensor(bias)).expand([1, self.layers[ii].out_features]).to_event(2))
            else:
                print('Invalid layer!')

            self.activations.append(layers[layer]['activation'])

        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()

    def forward(self, t, data=None):
        t = t.T
        
        for ii in range(self.n_layers):
            if self.activations[ii] == 'tanh':
                t = self.tanh(self.layers[ii](t))
        #        print(f'layer {ii}, weight: { self.layers[ii].weight.shape}, bias: { self.layers[ii].bias.shape}')
            elif self.activations[ii] == 'relu':
                t = self.relu(self.layers[ii](t))
        #        print(f'layer {ii}, weight: { self.layers[ii].weight.shape}, bias: { self.layers[ii].bias.shape}')
            else:
                t = self.layers[ii](t)
        #        print(f'layer {ii}, weight: { self.layers[ii].weight.shape}, bias: { self.layers[ii].bias.shape}')
        
        #y_hat = t.view(-1)
        t = t.reshape(n, n)
        #y_hat = t.T
        y_hat = conv.forward(t.detach().numpy()).flatten()
        #print(f' yhat: {y_hat.shape}')
        y_hat = torch.tensor(y_hat)
        sigma = pyro.param("sigma", torch.tensor(0.01))

        if data != None:
            with pyro.plate("data", len(data.reshape(-1))):
                obs = pyro.sample("obs", dist.Normal(y_hat, sigma), obs=data.reshape(-1))

        return t.reshape(-1)


def generate_the_problem(sigma_noise: float):

    radius = 0.3  # Radius of the circle

    x_input = np.linspace(0, 1, n).reshape(n, 1)
    y_input = np.linspace(0, 1, n).reshape(n, 1)
    x_coord, y_coord = np.meshgrid(x_input, y_input)

    center_x, center_y = 0.5, 0.5
    distance_from_center = np.sqrt((x_coord - center_x) ** 2 + (y_coord - center_y) ** 2)
    circle_image = np.where(distance_from_center <= radius, 1., 0.)
    
    PSF_size = 6
    PSF_param = 2.
    BC = 'wrap'
    deconv = Deconvolution_2D(PSF_size, PSF_param, BC)
    
    
    true = circle_image
    temp = deconv.forward(true)
    data = temp + np.random.normal(0, sigma_noise, true.shape)
    
    plt.figure()
    plt.imshow(true)
    plt.savefig('codes/image.png')
    plt.figure()
    plt.imshow(data)
    plt.savefig('codes/circle_image.png')
    return x_coord, y_coord, true, data, deconv


def generate_bnn_realization_plot(bnn, t):
    # Generate prior realizations, A not used
    realizations = np.empty((t.shape[1], 10))
    for ii in range(0, 10):
        realizations[:,ii] = bnn.forward(t)
    plt.figure()
    plt.plot(realizations)
    plt.savefig('realization.png')


def training_bnn_cpu(config, t, data, conv):
    # Set Pyro random seed
    pyro.set_rng_seed(config['training_parameters']['random_seed'])

    n_input = 2
    n_output = 1
    
    # Define Pyro BNN object and training parameters
    bnn_model = BNN(n_in=n_input,
                    n_out=n_output,
                    layers=config['bnn']['layers'],
                    conv=conv)
    guide = AutoDiagonalNormal(bnn_model)
    adam_params = {"lr": config['training_parameters']['learning_rate'],
                "betas": (0.95, 0.999)}
    optimizer = Adam(adam_params)
    svi = pyro.infer.SVI(bnn_model, guide, optimizer, loss=Trace_ELBO())
    num_iterations = config['training_parameters']['svi_num_iterations']
    progress_bar = trange(num_iterations)
    
    print(f'min {data.min()}, max {data.max()}')
    generate_bnn_realization_plot(bnn_model, t)
    for j in progress_bar:
        loss = svi.step(t, data)
        progress_bar.set_description("[iteration %04d] loss: %.4f" % (j + 1, loss / len(t)))

    # Get predictions for the solution
    predictive = pyro.infer.Predictive(bnn_model, guide=guide, num_samples=2000, return_sites=["_RETURN", "sigma"])
    preds = predictive(t)
    x_preds = preds['_RETURN']

    print(f'min {x_preds.min()}, max {x_preds.max()}')
    print(x_preds.shape)
    
    return x_preds


def calculate_mean_and_quantile(x_preds):
    x_mean = torch.mean(x_preds, axis=0)
    
    #lower_quantile = torch.quantile(x_preds, 0.05, axis=0)
    #upper_quantile = torch.quantile(x_preds, 0.95, axis=0)

    lower_quantile = az.hdi(x_preds.detach().numpy(), hdi_prob=0.95)
    
    upper_quantile = lower_quantile[:,1]
    lower_quantile = lower_quantile[:,0]

    return x_mean, lower_quantile, upper_quantile


def plot_results(config, true, data, x_preds, conv):
    x_mean, lower_quantile, upper_quantile = calculate_mean_and_quantile(x_preds)
    plt.figure()
    plt.plot(true.flatten(), label='true')
    plt.plot(data.flatten(), label='data')
    line, = plt.plot(x_mean, label='bnn')
    plt.fill_between(np.arange(0, len(true.flatten())),lower_quantile, upper_quantile, color=line.get_color(), alpha=0.5, label='90% quantile range')
    plt.plot(np.arange(0, len(true.flatten())), x_preds[0:2,:].T)
    plt.savefig('codes/flattened.png')
    print(f'X {x_preds.shape}')

    plt.figure()
    plt.imshow(x_mean.reshape(n, n))
    plt.show()
    plt.savefig(f"plots/{config['name']}_solution.png")
    plt.figure()
    plt.imshow(conv.forward(x_mean.reshape(n, n)))
    plt.show()
    plt.savefig(f"plots/{config['name']}_forward.png")
    


def plot_problem(config, true, y):
    plt.figure()
    plt.imshow(true.reshape(n, n))
    plt.show()

    plt.savefig(f"plots/{config['name']}_true.png")
    
    plt.figure()
    plt.imshow(y.reshape(n, n))
    plt.show()

    plt.savefig(f"plots/{config['name']}_problem.png")



if __name__ == '__main__':
    # Parse the config argument
    
    parser = argparse.ArgumentParser(description="1D-deconvolution solving with BNN prior")
    parser.add_argument('--file', type=str, required=True, help='config file to use')
    parser.add_argument('--config', type=str, required=True, help='config to use')
    args = parser.parse_args()
    config = yaml.safe_load(open(f"codes/config/{args.file}"))[args.config]
    
    # Define if trained on cpu or gpu
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    device = 'cpu'
    torch.set_default_device(device)
    
    # Get the initial parameters from the config file
    #n_t = config['n_t']
    #n_y = config['n_y']
    #domain = config['domain']
    sigma_noise = config['sigma_noise']
    
    np.random.seed(42)
    
    x, y, true, data, conv = generate_the_problem(sigma_noise)

    t = torch.tensor(np.concatenate((x.reshape(1, -1), y.reshape(1, -1)), axis=0), dtype=torch.float32)
    true = torch.tensor(true)
    data = torch.tensor(data)

    np.savetxt("foo.csv", t.T, delimiter=",")

    # Convert data to PyTorch tensors

    x_preds = training_bnn_cpu(config, t, data, conv)
    
    '''
    results = dict({'t': t,
                            'x': x,
                            'true': true,
                            'data': data,
                            'x_preds': x_preds})
    
    with open(f'results/{config['name']}.pickle', 'wb') as handle:
        pickle.dump(results, handle, protocol=pickle.HIGHEST_PROTOCOL)
    '''
    
    plot_problem(config, true, data)
    plot_results(config, true, data, x_preds, conv)
    

    
        