# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wrapper to accelerate iterative solver with Anderson."""

from typing import Any
from typing import Callable
from typing import Optional
from typing import NamedTuple
from typing import Union

from dataclasses import dataclass

import jax
import jax.numpy as jnp

from jaxopt._src import base
from jaxopt._src.tree_util import tree_l2_norm, tree_sub
from jaxopt._src.anderson import AndersonAcceleration
from jaxopt._src.anderson import build_history, anderson_step, update_history


class AndersonWrapperState(NamedTuple):
  """Named tuple containing state information.

  Attributes:
    iter_num: iteration number
    solver_state: state of the solver wrapped
    error: residuals of current estimate
    params_history: history of previous anderson iterates
    residuals_history: residuals of previous iterates
      fixed_point_fun(params_history) - params_history
    residual_gram: Gram matrix: G.T @ G with G the matrix of residuals
      each column of G is a flattened pytree of residuals_history
  """
  iter_num: int
  solver_state: Any
  error: float
  params_history: Any
  residuals_history: Any
  residual_gram: jnp.array


@dataclass
class AndersonWrapper(base.IterativeSolver):
  """Wrapper of AndersonAcceleration to accelerate other solvers with jaxopt-compatible interface.

  Note that the internal solver state can be accessed via the ``aux`` attribute of AndersonState.

  Attributes:
    solver: solver object to accelerate. Must exhibit init() and update() methods.
    history_size: size of history. Affect memory cost. (default: 5).
    beta: momentum in Anderson updates. (default: 1).
    ridge: ridge regularization in solver.
      Consider increasing this value if the solver returns ``NaN``.
    verbose: whether to print error on every iteration or not.
      Warning: verbose=True will automatically disable jit.
    implicit_diff: whether to enable implicit diff or autodiff of unrolled
      iterations.
    implicit_diff_solve: the linear system solver to use.
    jit: whether to JIT-compile the optimization loop (default: "auto").
    unroll: whether to unroll the optimization loop (default: "auto")
  """
  solver: base.IterativeSolver
  history_size: int = 5
  beta: float = 1.
  ridge: float = 1e-5
  verbose: bool = False
  implicit_diff: bool = True
  implicit_diff_solve: Optional[Callable] = None
  jit: base.AutoOrBoolean = "auto"
  unroll: base.AutoOrBoolean = "auto"

  def init(self, init_params, *args, **kwargs) -> base.OptStep:
    params, solver_state = self.solver.init(init_params, *args, **kwargs)
    history = [params]
    for _ in range(self.history_size):
      params, solver_state = self.solver.update(params, solver_state, *args, **kwargs)
      history.append(params)

    params_history, residuals_history, residual_gram = build_history(history)
    error = solver_state.error

    state = AndersonWrapperState(iter_num=0,
                                 solver_state=solver_state,
                                 error=error,
                                 params_history=params_history,
                                 residuals_history=residuals_history,
                                 residual_gram=residual_gram)
    return base.OptStep(params=params, state=state)

  def update(self, params, state, *args, **kwargs) -> base.OptStep:
    """Perform one step of Anderson acceleration over the internal solver update.

    The reset_state attribute is used to update the internal solver state after
    the Anderson step.

    Args:
      params: parameters optimized by solver. Only its pytree structure matters (content unused).
      state: AndersonWrapperState
        Crucially, state.params_history and state.residuals_history are the sequences used to generate next iterate.
        Note: state.solver_state is the internal solver state.
      args,kwargs: additional parameters passed to ``update`` method of internal solver
        Note: sometimes those are hyper-parameters of the solver, but if the solver is a Jaxopt solver
        they will be forwarded to the underlying function being optimized
    """
    del params
    params_history = state.params_history
    residuals_history = state.residuals_history
    residual_gram = state.residual_gram
    pos = jnp.mod(state.iter_num, self.history_size)

    extrapolated = anderson_step(params_history, residuals_history,
                                 residual_gram, self.ridge, self.beta)
    extrapolated, solver_state = self.solver.init(extrapolated, *args, **kwargs)
    next_params, solver_state = self.solver.update(extrapolated, solver_state, *args, **kwargs)

    residual = tree_sub(next_params, extrapolated)
    ret = update_history(pos, params_history, residuals_history,
                         residual_gram, extrapolated, residual)
    params_history, residuals_history, residual_gram, error = ret

    next_state = AndersonWrapperState(iter_num=state.iter_num+1,
                                      solver_state=solver_state,
                                      error=solver_state.error,  
                                      params_history=params_history,
                                      residuals_history=residuals_history,
                                      residual_gram=residual_gram)
    return base.OptStep(params=next_params, state=next_state)

  def optimality_fun(self, params, *args, **kwargs):
    """Optimality function mapping compatible with ``@custom_root``."""
    #TODO(lbethune): should we use internal solver optimality_fun or Anderson criterion ?
    return self.solver.optimality_fun(params, *args, **kwargs)

  def __post_init__(self):
    self.maxiter = self.solver.maxiter - self.history_size
    if self.maxiter < 0:
      raise ValueError('Ensure maxiter is greater than history_size otherwise acceleration is impossible.')
    self.tol = self.solver.tol