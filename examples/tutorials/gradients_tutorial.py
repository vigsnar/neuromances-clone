"""
Gradients of Variables and Constraints tutorial

This script demonstrates how to differentiate NeuroMANCER variables and constraints

"""

import neuromancer as nm
import torch
from neuromancer.constraint import Variable, Constraint
from neuromancer import policies
from neuromancer.gradients import gradient


"""
compute gradients of the constraints w.r.t. tensors from the dataset
"""

# Let's define a neuromancer variable
x = Variable('x')
# Let's create a dataset dictionary with randomly sampled datapoints for variable x
# requires_grad needs to be true if we want to compute gradients w.r.t. variable x
data = {'x': torch.rand([2,3], requires_grad=True)}
# and define new constant variable with given value
a = Variable('a', value=1.5)
# now let's create new variable as algebraic expression of variables
math_exp_var = (3*x + 1 - 0.5 * a)**2
# evaluate expression on a dataset with sampled variable x
print(math_exp_var(data))
# now we create new constraint with 2-norm penalty on constraints violations
cnstr = (math_exp_var < 2.0)^2
# and evaluate its aggregate violations on dataset with random variable x
cnstr(data)

# obtain gradients of the constraints w.r.t. inputs via backprop - not a prefered option
cnstr(data).backward()
dc_dx = data['x'].grad
print(dc_dx)
# obtain gradients of the constraints w.r.t. inputs via pytorch's grad functio
con_grad = torch.autograd.grad(cnstr(data), data['x'])
print(con_grad[0])


"""
compute gradients of the constraints w.r.t. tensor inputs generated by the component model
"""
# Let's create a dataset dictionary with randomly sampled datapoints for parameter p
nsim = 20
data2 = {'p': torch.rand([nsim, 3], requires_grad=True)}
dims = {}
dims['p'] = data2['p'].shape
dims['U'] = (nsim, 2)  # defining expected dimensions of the solution variable: internal policy key 'U'
# create neural model
sol_map = policies.MLPPolicy(
    {**dims},
    hsizes=[10] * 2,
    input_keys=["p"],
    name='sol_map',
)
# define variable z as output of the neural model
z = Variable(f"U_pred_{sol_map.name}", name='z')
# now let's create new variable as algebraic expression of variables
math_exp_var1 = (10*z + 1)**2
# lets do a forward pass on the model
out = sol_map(data2)
# now we create new constraint with 2-norm penalty on constraints violations
cnstr1 = (math_exp_var1 < 1.0)^2
# and evaluate its aggregate violations on dataset with random variable x
print(cnstr1(out))

# obtain gradients of the constraints w.r.t. component outputs z
con3_grad_z = torch.autograd.grad(cnstr1(out), out[z.key])
print(con3_grad_z[0])
# obtain gradients of the constraints w.r.t. parameter inputs p
con3_grad_p = torch.autograd.grad(cnstr1(sol_map(data2)), data2['p'])
print(con3_grad_p[0])

"""
compute gradients of variables w.r.t. tensor inputs from the sampled dataset using nm.gradient function
"""
# evaluate expression on the model outputs z
print(math_exp_var1(out))
# obtain gradients of the variable w.r.t. component outputs z
var_grad_z = gradient(math_exp_var1(out), out[z.key])
print(var_grad_z)
# obtain gradients of the variable w.r.t. data parameters p
var_grad_p = gradient(math_exp_var1(out), data2['p'])
print(var_grad_p)

"""
compute gradients of components w.r.t. tensor inputs from the sampled dataset using nm.gradient function
"""
# obtain gradients of the component outputs w.r.t. data parameters p
comp_grad_p = gradient(sol_map(data2)[z.key],  data2['p'])
print(comp_grad_p)