from scipy.io import loadmat

import torch
import torch.nn as nn
import torch.nn.functional as F
from linear import SVDLinear, PerronFrobeniusLinear, NonnegativeLinear, SpectralLinear


def heat_flow(m_flow, dT):
    U = 1.1591509722222224 * m_flow * dT
    return U


class HeatFlow(nn.Module):

    def __init__(self):
        super().__init__()
        self.rho = torch.nn.Parameter(torch.tensor(0.997), requires_grad=False)  # density  of water kg/1l
        self.cp = torch.nn.Parameter(torch.tensor(4185.5),
                                     requires_grad=False)  # specific heat capacity of water J/(kg/K)
        self.time_reg = torch.nn.Parameter(torch.tensor(1 / 3600), requires_grad=False)

    def forward(self, m_flow, dT):
        return m_flow * self.rho * self.cp * self.time_reg * dT


class MLPHeatFlow(nn.Module):
    def __init__(self, insize, outsize, hiddensize, bias=False, nonlinearity=F.gelu):
        super().__init__()
        self.layer1 = nn.Linear(insize, hiddensize, bias=bias)
        self.layer2 = nn.Linear(hiddensize, outsize, bias=bias)
        self.nlin = nonlinearity

    def __call__(self, m_flow, dT):
        return self.nlin(self.layer2(self.nlin(self.layer1((torch.cat([m_flow, dT], dim=1))))))


class SSM(nn.Module):
    def __init__(self, nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=False, heatflow='white',
                 xmin=0, xmax=35, umin=-5000, umax=5000,
                 Q_dx=1e2, Q_dx_ud=1e5, Q_con_x=1e1, Q_con_u=1e1, Q_spectral=1e2):
        super().__init__()
        self.nx, self.ny, self.nu, self.nd, self.n_hidden = nx, ny, nu, nd, n_hidden
        self.A = nn.Linear(nx, nx, bias=bias)
        self.B = nn.Linear(nu, nx, bias=bias)
        self.E = nn.Linear(nd, nx, bias=bias)
        self.C = nn.Linear(nx, ny, bias=bias)
        self.x0_correct = torch.nn.Parameter(torch.zeros(1, nx)) # state initialization term, corresponding to G

        if heatflow == 'white':
            self.heat_flow = heat_flow
        elif heatflow == 'gray':
            self.heat_flow = HeatFlow()
        elif heatflow == 'black':
            self.heat_flow = MLPHeatFlow(n_m + n_dT, nu, n_hidden, bias=self.bias)

        #  Regularization Initialization
        dxmax_val = 0.5
        dxmin_val = -0.5
        self.xmin, self.xmax, self.umin, self.umax = xmin, xmax, umin, umax
        self.dxmax = nn.Parameter(dxmax_val * torch.ones(1, nx), requires_grad=False)
        self.dxmin = nn.Parameter(dxmin_val * torch.ones(1, nx), requires_grad=False)
        self.sxmin, self.sxmax, self.sumin, self.sumax, self.sdx_x, self.Sdx_u, self.Sdx_d, self.spectral_error = [[] for i in range(8)]
        # weights on one-step difference of states
        self.Q_dx = Q_dx / n_hidden  # penalty on smoothening term dx
        self.Q_dx_ud = Q_dx_ud / n_hidden  # penalty on constrained maximal influence of u and d on x
        # state and input constraints weight
        self.Q_con_x = Q_con_x / n_hidden
        self.Q_con_u = Q_con_u / nu
        # (For SVD) Weights on orthogonality violation of matrix decomposition factors
        self.Q_spectral = Q_spectral

    def regularize(self, x_0, x, u, d):
        # Barrier penalties
        self.sxmin.append(F.relu(-x + self.xmin))
        self.sxmax.append(F.relu(x - self.xmax))
        self.sumin.append(F.relu(-u + self.umin))
        self.sumax.append(F.relu(u - self.umax))
        # one step state residual penalty
        self.sdx_x.append(x - x_0)
        # penalties on max one-step infuence of controls and disturbances on states
        self.dx_u.append(F.relu(-self.B(u) + self.dxmin) + F.relu(self.B(u) - self.dxmax))
        self.dx_d.append(F.relu(-self.E(d) + self.dxmin) + F.relu(self.E(d) - self.dxmax))

    def regularization_error(self):
        sxmin, sxmax, sumin, sumax = (torch.stack(self.sxmin), torch.stack(self.sxmax),
                                      torch.stack(self.umin), torch.stack(self.sumax))
        xmin_loss = self.Q_con_x*F.mse_loss(sxmin,  self.xmin * torch.ones(sxmin.shape))
        xmax_loss = self.Q_con_x*F.mse_loss(sxmax,  self.xmax * torch.ones(sxmax.shape))
        umin_loss = self.Q_con_u*F.mse_loss(sumin, self.umin * torch.ones(sumin.shape))
        umax_loss = self.Q_con_u*F.mse_loss(sumax, self.umax * torch.ones(sumax.shape))
        sdx, dx_u, dx_d = torch.stack(self.sdx_x), torch.stack(self.du_u), torch.stack(self.dx_d)
        sdx_loss = self.Q_dx*F.mse_loss(sdx, torch.zeros(sdx.shape))
        dx_u_loss = self.Q_dx_ud*F.mse_loss(dx_u, torch.zeros(dx_u.shape))
        dx_d_loss = self.Q_dx_ud*F.mse_loss(dx_d, torch.zeros(dx_d.shape))
        return torch.sum(torch.stack(xmin_loss, xmax_loss, umin_loss, umax_loss, sdx_loss, dx_u_loss, dx_d_loss))

    def forward(self, x, M_flow, DT, D):
        """
        """
        X, Y, U = [], [], []
        x = x + self.x0_correct
        for m_flow, dT, d in zip(M_flow, DT, D):
            x_prev = x  # previous state memory
            u = self.heatflow(torch.cat([m_flow, dT], dim=1))
            x = self.A(x) + self.B(u) + self.E(d)
            y = self.C(x)
            X.append(x)
            Y.append(y)
            U.append(u)
            self.regularize(x_prev, x, u, d)
        return torch.stack(X), torch.stack(Y), torch.stack(U), self.regularization_error()


class SVDSSM(SSM):
    def __init__(self, nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=False, heatflow='white',
                 xmin=0, xmax=35, umin=-5000, umax=5000,
                 Q_dx=1e2, Q_dx_ud=1e5, Q_con_x=1e1, Q_con_u=1e1, Q_spectral=1e2):
        super().__init__(nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=bias, heatflow=heatflow,
                         xmin=xmin, xmax=xmax, umin=umin, umax=umax,
                         Q_dx=Q_dx, Q_dx_ud=Q_dx_ud, Q_con_x=Q_con_x, Q_con_u=Q_con_u, Q_spectral=Q_spectral)
        # Module initialization
        self.A = SVDLinear(nx, nx, bias=bias, sigma_min=0.6, sigma_max=1.0)
        self.E = PerronFrobeniusLinear(nd, nx, bias=bias, sigma_min=0.05, sigma_max=1)
        self.B = NonnegativeLinear(nu, nx, bias=bias)
        self.C = SVDLinear(nx, ny, bias=bias, sigma_min=0.9, sigma_max=1)

    def forward(self, x, M_flow, DT, D):
        """
        """
        X, Y, U, regularization_error = super().forward(x, M_flow, DT, D)
        spectral_error = self.A.spectral_error, self.C.spectral_error
        return X, Y, U, self.regularization_error + spectral_error


class PerronFrobeniusSSM(SSM):
    def __init__(self, nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=False, heatflow='white',
                 xmin=0, xmax=35, umin=-5000, umax=5000,
                 Q_dx=1e2, Q_dx_ud=1e5, Q_con_x=1e1, Q_con_u=1e1, Q_spectral=1e2):
        super().__init__(nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=bias, heatflow=heatflow,
                         xmin=xmin, xmax=xmax, umin=umin, umax=umax,
                         Q_dx=Q_dx, Q_dx_ud=Q_dx_ud, Q_con_x=Q_con_x, Q_con_u=Q_con_u, Q_spectral=Q_spectral)
        # Module initialization
        self.A = PerronFrobeniusLinear(nx, nx, bias=bias, sigma_min=0.95, sigma_max=1.0)
        self.E = PerronFrobeniusLinear(nd, nx, bias=bias, sigma_min=0.05, sigma_max=1)
        self.B = NonnegativeLinear(nu, nx, bias=bias)
        self.C = PerronFrobeniusLinear(nx, ny, bias=bias, sigma_min=0.9, sigma_max=1)


class SpectralSSM(PerronFrobeniusSSM):
    def __init__(self, nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=False, heatflow='white',
                 xmin=0, xmax=35, umin=-5000, umax=5000,
                 Q_dx=1e2, Q_dx_ud=1e5, Q_con_x=1e1, Q_con_u=1e1, Q_spectral=1e2):
        super().__init__(nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=bias, heatflow=heatflow,
                         xmin=xmin, xmax=xmax, umin=umin, umax=umax,
                         Q_dx=Q_dx, Q_dx_ud=Q_dx_ud, Q_con_x=Q_con_x, Q_con_u=Q_con_u, Q_spectral=Q_spectral)
        self.A = SpectralLinear(nx, nx, bias=bias, reflector_size=1, sig_mean=0.8, r=0.2)


class SSMGroundTruth(SSM):
    # TODO: Test to see if corresponds with ground truth
    def __init__(self, nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=False,
                 heatflow='white', # dummy args for common API in training script
                 xmin=0, xmax=35, umin=-5000, umax=5000,
                 Q_dx=1e2, Q_dx_ud=1e5, Q_con_x=1e1, Q_con_u=1e1, Q_spectral=1e2):
        super().__init__(nx, ny, n_m, n_dT, nu, nd, n_hidden, bias=bias)
        file = loadmat('./Matlab_matrices/Reno_model_for_py.mat')  # load disturbance file
        # reduced order linear model
        A = file['Ad_ROM']
        B = file['Bd_ROM']
        C = file['Cd_ROM']
        E = file['Ed_ROM']
        G = file['Gd_ROM']
        F = file['Fd_ROM']
        self.G = torch.tensor(G)
        self.F = torch.tensor(F)

        with torch.no_grad():
            self.A.weight.copy_(torch.tensor(A))
            self.B.weight.copy_(torch.tensor(B))
            self.E.weight.copy_(torch.tensor(E))
            self.C.weight.copy_(torch.tensor(C))

        for p in self.parameters():
            p.requires_grad = False

    @property
    def regularization_error(self):
        return 0.0

    def forward(self, x, M_flow, DT, D):
        """
        """
        X, Y, U = [], [], []
        for m_flow, dT, d in zip(M_flow, DT, D):
            x_prev = x  # previous state memory
            u = self.heatflow(torch.cat([m_flow, dT], dim=1))
            x = self.A(x) + self.B(u) + self.E(d) + self.G
            y = self.C(x) + self.F - 273.15
            X.append(x)
            Y.append(y)
            U.append(u)
            self.regularize(x_prev, x, u, d)
        return torch.stack(X), torch.stack(Y), torch.stack(U), self.regularization_error

