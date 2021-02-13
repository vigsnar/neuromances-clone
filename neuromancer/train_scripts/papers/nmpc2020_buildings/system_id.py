"""
Script for training block dynamics models for system identification.

Basic model options are:
    + prior on the linear maps of the neural network
    + state estimator
    + non-linear map type
    + hidden state dimension
    + Whether to use affine or linear maps (bias term)
Basic data options are:
    + Load from a variety of premade data sequences
    + Load from a variety of emulators
    + Normalize input, output, or disturbance data
    + Nstep prediction horizon
Basic optimization options are:
    + Number of epochs to train on
    + Learn rate
Basic logging options are:
    + print to stdout
    + mlflow
    + weights and bias

More detailed description of options in the `get_base_parser()` function in common.py.
"""
import argparse

import torch
import slim
import neuromancer.blocks as blocks
from neuromancer.visuals import VisualizerOpen, VisualizerTrajectories
from neuromancer.trainer import Trainer
from neuromancer.problem import Problem
from neuromancer.simulators import OpenLoopSimulator
from common import load_dataset, get_logger
import psl
import numpy as np
import matplotlib as plt

from setup_system_id import (
    get_model_components,
    get_objective_terms,
    get_parser
)


if __name__ == "__main__":
    args = get_parser().parse_args()
    args.bias = False
    print({k: str(getattr(args, k)) for k in vars(args) if getattr(args, k)})
    device = f"cuda:{args.gpu}" if args.gpu is not None else "cpu"

    logger = get_logger(args)

    emul = psl.emulators[args.system]()
    umin = np.concatenate([emul.mf_min, emul.dT_min[0]])
    umax = np.concatenate([emul.mf_max, emul.dT_max[0]])
    norm_bounds = {"U": {'min': umin, 'max': umax}}
    dataset = load_dataset(args, device, "openloop", reduce_d=True, norm_bounds=norm_bounds)
    print(dataset.dims)

    estimator, dynamics_model = get_model_components(args, dataset)
    objectives, constraints = get_objective_terms(args, dataset, estimator, dynamics_model)

    model = Problem(objectives, constraints, [estimator, dynamics_model])
    model = model.to(device)

    simulator = OpenLoopSimulator(model=model, dataset=dataset, eval_sim=not args.skip_eval_sim)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    trainer = Trainer(
        model,
        dataset,
        optimizer,
        logger=logger,
        simulator=simulator,
        epochs=args.epochs,
        eval_metric=args.eval_metric,
        patience=args.patience,
        warmup=args.warmup,
    )

    best_model = trainer.train()
    best_outputs = trainer.evaluate(best_model)

    visualizer = VisualizerOpen(
        dataset,
        dynamics_model,
        args.verbosity,
        args.savedir,
        training_visuals=args.train_visuals,
        trace_movie=args.trace_movie,
    )
    plots = visualizer.eval(best_outputs)

    logger.log_artifacts(plots)
    logger.clean_up()


plt.pyplot.figure()
CA = np.matmul(dynamics_model.fy.linear.weight.detach().numpy(),
               dynamics_model.fx.linear.weight.detach().numpy())
CAB = np.matmul(CA, dynamics_model.fu.linear.effective_W().detach().numpy().T)
plt.pyplot.imshow(CAB)

plt.pyplot.figure()
CA = np.matmul(emul.C,emul.A)
CAB = np.matmul(CA,emul.B)
plt.pyplot.imshow(CAB)