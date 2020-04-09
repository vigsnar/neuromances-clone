import os
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('-gpu', type=int, default=0)
args = parser.parse_args()

for ssm in ['vanilla', 'pf', 'svd', 'spectral', 'true']:
    for heatflow in ['black', 'grey', 'white']:
        for state_estimator in ['true', 'linear', 'pf', 'mlp', 'rnn', 'rnn_constr', 'rnn_spectral', 'rnn_svd', 'kf']:
            print(f'\n###############\n{ssm} {heatflow} {state_estimator}\n#################\n')
            err = os.system(f'python train.py --gpu {args.gpu} -ssm_type {ssm} -heatflow {heatflow} -state_estimator {state_estimator}')
            if err != 0:
                with open('test_all.log', 'a') as log:
                    log.write(f'{ssm} {heatflow} {state_estimator}\n')
