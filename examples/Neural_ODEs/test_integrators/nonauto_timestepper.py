import os

import numpy as np
import matplotlib.pyplot as plt
from scipy.integrate import odeint

from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.optim as optim
import torch
import torch.nn as nn

import psl

from neuromancer.integrators import integrators
from neuromancer.problem import Problem
from neuromancer.callbacks import Callback
from neuromancer.loggers import MLFlowLogger
from neuromancer.trainer import Trainer
from neuromancer.constraint import Loss
from neuromancer.component import Component
from neuromancer.blocks import MLP
from neuromancer.activations import activations
from neuromancer.dataset import DictDataset
from auto_timestepper import plot_traj, TSCallback, truncated_mse


def get_x0(box):
    """
    Randomly sample an initial condition

    :param box: Dictionary with keys 'min' and 'max' and values np.arrays with shape=(nx,)
    """
    return np.random.uniform(low=box['min'], high=box['max'])


def get_box(system, ts, nsim):
    """
    Get a hyperbox defined by min and max values on each of nx axes. Used to sample initial conditions for simulations.
    Box is generated by simulating system with step size ts for nsim steps and then taking the min and max along each axis

    :param system: (psl.ODE_NonAutonomous)
    :param ts: (float) Timestep interval size
    :param nsim: (int) Number of simulation steps to use in defining box
    """
    sim = system.simulate(ts=ts, nsim=nsim, U=system.get_U(nsim))['X']
    return {'min': sim.min(axis=0), 'max': sim.max(axis=0)}


class Validator:

    def __init__(self, netG, sys, box):
        """
        Used for evaluating model performance

        :param netG: (nn.Module) Some kind of neural network state space model
        :param sys: (psl.ODE_NonAutonomous) Ground truth ODE system
        :param box: (dict) Dictionary with 'min', 'max' keys and np.array values for sampling initial conditions
        """
        self.x0s = [get_x0(box) for i in range(10)]
        X, U = [], []
        for x0 in self.x0s:
            sim = sys.simulate(ts=args.ts, nsim=1000, x0=x0, U=sys.get_U(1000))
            X.append(sim['X']), U.append(sim['U'])

        self.reals = {'X': torch.tensor(np.stack(X), dtype=torch.float32),
                      'U': torch.tensor(np.stack(U), dtype=torch.float32)}
        self.netG = netG

    def __call__(self):
        nsteps = self.netG.nsteps
        self.netG.nsteps = 1000
        with torch.no_grad():
            simulation = self.netG.forward(self.reals)
            simulation = np.nan_to_num(simulation['X_ssm'].detach().numpy(),
                                       copy=True, nan=200000., posinf=None, neginf=None)
            mses = ((self.reals['X'] - simulation)**2).mean(axis=(1, 2))
            truncs = truncated_mse(self.reals['X'], simulation)
        best = np.argmax(truncs)
        self.netG.nsteps = nsteps
        return truncs.mean(), mses.mean(), simulation[best], self.reals['X'][best]


def get_data(nsteps, box, sys):
    """
    :param nsteps: (int) Number of timesteps for each batch of training data
    :param box: (dict) Dictionary with 'min', 'max' keys and np.array values for sampling initial conditions
    :param sys: (psl.ODE_NonAutonomous)

    """
    X, U, T = [], [], []
    for _ in range(args.nsim):
        sim = sys.simulate(ts=args.ts, nsim=nsteps, x0=get_x0(box), U=sys.get_U(nsteps))
        X.append(sim['X'])
        U.append(sim['U'])
                
    X, U = np.stack(X), np.stack(U)
    sim = sys.simulate(ts=args.ts, nsim=args.nsim*nsteps, x0=get_x0(box), U=sys.get_U(args.nsim*nsteps))

    nx, nu = X.shape[-1], U.shape[-1]
    x, u  = sim['X'].reshape(args.nsim, nsteps, nx), sim['U'].reshape(args.nsim, nsteps, nu)
    X, U = np.concatenate([X, x], axis=0), np.concatenate([U, u], axis=0)

    train_data = DictDataset({'X': torch.Tensor(X, device=device),
                              'U': torch.Tensor(U, device=device)}, name='train')
    train_loader = DataLoader(train_data, batch_size=args.batch_size,
                              collate_fn=train_data.collate_fn, shuffle=True)

    dev_data = DictDataset({'X': torch.Tensor(X[0:1], device=device),
                            'U': torch.Tensor(U[0:1], device=device)}, name='dev')
    dev_loader = DataLoader(dev_data, num_workers=1, batch_size=args.batch_size,
                            collate_fn=dev_data.collate_fn, shuffle=False)
    test_loader = dev_loader
    return nx, nu, train_loader, dev_loader, test_loader


class SSMIntegrator(Component):
    """
    Component state space model wrapper for integrator
    """
    def __init__(self, integrator, nsteps):
        """
        :param integrator: (neuromancer.integrators.Integrator)
        :param nsteps: (int) Number of rollout steps from initial condition
        """
        super().__init__(['X'], ['X_ssm'], name='ssm')
        self.integrator = integrator
        self.nsteps = nsteps

    def forward(self, data):
        """
        :param data: (dict {str: Tensor}) {'U': shape=(nsamples, nsteps, nu), 'X': shape=(nsamples, nsteps, nx)}
        
        """
        
        x = data['X'][:, 0, :]
        U = data['U']
        X = [x]
        for i in range(self.nsteps - 1):
            x = self.integrator(x, u=U[:, i, :])
            X.append(x)
        return {'X_ssm': torch.stack(X, dim=1)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-epochs', type=int, default=1000,
                        help='Number of epochs of training.')
    parser.add_argument('-system', choices=[k for k in psl.nauto_ode], default='LorenzControl')
    parser.add_argument('-lr', type=float, default=0.001)
    parser.add_argument('-nsteps', type=int, default=4)
    parser.add_argument('-stepper', default='Euler', choices=[k for k in integrators])
    parser.add_argument('-batch_size', type=int, default=100)
    parser.add_argument('-nsim', type=int, default=1000)
    parser.add_argument('-ts', type=float, default=0.01)
    parser.add_argument('-q_mse', type=float, default=2.0)
    parser.add_argument('-logdir', default='test')
    parser.add_argument("-exp", type=str, default="test",
           help="Will group all run under this experiment name.")
    parser.add_argument("-location", type=str, default="mlruns",
           help="Where to write mlflow experiment tracking stuff")
    parser.add_argument("-run", type=str, default="neuromancer",
           help="Some name to tell what the experiment run was about.")
    parser.add_argument('-hsize', type=int, default=128, help='Size of hiddens states')
    parser.add_argument('-nlayers', type=int, default=4, help='Number of hidden layers for MLP')

    args = parser.parse_args()
    device = torch.device('cuda:0' if torch.cuda.is_available() else "cpu")
    os.makedirs(args.logdir, exist_ok=True)

    sys = psl.nauto_ode[args.system]()
    box = get_box(sys, args.ts, 1000)
    nx, nu, train_data, dev_data, test_data = get_data(args.nsteps, box, sys)
    fx = MLP(nx+nu, nx, bias=False, linear_map=nn.Linear, nonlin=activations['elu'], hsizes=[args.hsize for h in range(args.nlayers)])
    interp_u = lambda tq, t, u: u
    integrator = integrators[args.stepper](fx, h=args.ts, interp_u=interp_u)
    ssm = SSMIntegrator(integrator, nsteps=args.nsteps)
    opt = optim.Adam(ssm.parameters(), args.lr, betas=(0.0, 0.9))
    validator = Validator(ssm, sys, box)
    callback = TSCallback(validator, args.logdir)
    objective = Loss(['X', 'X_ssm'], F.mse_loss, weight=args.q_mse, name='mse')
    problem = Problem([ssm], objective)
    logger = MLFlowLogger(args, savedir=args.logdir, stdout=['train_mse', 'eval_mse', 'eval_tmse'])
    trainer = Trainer(problem, train_data, dev_data, test_data, opt, logger,
                                  callback=callback,
                                  epochs=args.epochs,
                                  patience=args.epochs,
                      train_metric='train_mse',
                      dev_metric='dev_mse',
                      test_metric='test_mse',
                      eval_metric='eval_tmse')

    lr = args.lr
    nsteps = args.nsteps
    for i in range(5):
        print(f'training {nsteps} objective, lr={lr}')
        trainer.train()
        lr/= 2.0
        nsteps *= 2
        nx, nu, train_data, dev_data, test_data = get_data(nsteps, box, sys)
        trainer.train_data, trainer.dev_data, trainer.test_data = train_data, dev_data, test_data
        ssm.nsteps = nsteps
        opt.param_groups[0]['lr'] = lr





