# Copyright 2022 The JAX Authors.
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

from __future__ import annotations

import builtins
from collections.abc import Callable, Sequence
from functools import partial
import math
import operator
from typing import overload, Any, Literal, Protocol, Union
import warnings

import numpy as np

from jax import lax
from jax._src import api
from jax._src import core
from jax._src import dtypes
from jax._src.numpy import ufuncs
from jax._src.numpy.util import (
    _broadcast_to, check_arraylike, _complex_elem_type,
    promote_dtypes_inexact, promote_dtypes_numeric, _where, implements)
from jax._src.lax import lax as lax_internal
from jax._src.typing import Array, ArrayLike, DType, DTypeLike, DeprecatedArg
from jax._src.util import (
    canonicalize_axis as _canonicalize_axis, maybe_named_axis,
    NumpyComplexWarning)


_all = builtins.all
_lax_const = lax_internal._const


Axis = Union[int, Sequence[int], None]

def _isscalar(element: Any) -> bool:
  if hasattr(element, '__jax_array__'):
    element = element.__jax_array__()
  return dtypes.is_python_scalar(element) or np.isscalar(element)

def _moveaxis(a: ArrayLike, source: int, destination: int) -> Array:
  # simplified version of jnp.moveaxis() for local use.
  check_arraylike("moveaxis", a)
  a = lax_internal.asarray(a)
  source = _canonicalize_axis(source, np.ndim(a))
  destination = _canonicalize_axis(destination, np.ndim(a))
  perm = [i for i in range(np.ndim(a)) if i != source]
  perm.insert(destination, source)
  return lax.transpose(a, perm)

def _upcast_f16(dtype: DTypeLike) -> DType:
  if np.dtype(dtype) in [np.float16, dtypes.bfloat16]:
    return np.dtype('float32')
  return np.dtype(dtype)

def _promote_integer_dtype(dtype: DTypeLike) -> DTypeLike:
  # Note: NumPy always promotes to 64-bit; jax instead promotes to the
  # default dtype as defined by dtypes.int_ or dtypes.uint.
  if dtypes.issubdtype(dtype, np.bool_):
    return dtypes.int_
  elif dtypes.issubdtype(dtype, np.unsignedinteger):
    if np.iinfo(dtype).bits < np.iinfo(dtypes.uint).bits:
      return dtypes.uint
  elif dtypes.issubdtype(dtype, np.integer):
    if np.iinfo(dtype).bits < np.iinfo(dtypes.int_).bits:
      return dtypes.int_
  return dtype


ReductionOp = Callable[[Any, Any], Any]

def _reduction(a: ArrayLike, name: str, np_fun: Any, op: ReductionOp, init_val: ArrayLike,
               *, has_identity: bool = True,
               preproc: Callable[[ArrayLike], ArrayLike] | None = None,
               bool_op: ReductionOp | None = None,
               upcast_f16_for_computation: bool = False,
               axis: Axis = None, dtype: DTypeLike | None = None, out: None = None,
               keepdims: bool = False, initial: ArrayLike | None = None,
               where_: ArrayLike | None = None,
               parallel_reduce: Callable[..., Array] | None = None,
               promote_integers: bool = False) -> Array:
  bool_op = bool_op or op
  # Note: we must accept out=None as an argument, because numpy reductions delegate to
  # object methods. For example `np.sum(x)` will call `x.sum()` if the `sum()` method
  # exists, passing along all its arguments.
  if out is not None:
    raise NotImplementedError(f"The 'out' argument to jnp.{name} is not supported.")
  check_arraylike(name, a)
  dtypes.check_user_dtype_supported(dtype, name)
  axis = core.concrete_or_error(None, axis, f"axis argument to jnp.{name}().")

  if initial is None and not has_identity and where_ is not None:
    raise ValueError(f"reduction operation {name} does not have an identity, so to use a "
                     f"where mask one has to specify 'initial'")

  a = a if isinstance(a, Array) else lax_internal.asarray(a)
  a = preproc(a) if preproc else a
  pos_dims, dims = _reduction_dims(a, axis)

  if initial is None and not has_identity:
    shape = np.shape(a)
    if not _all(shape[d] >= 1 for d in pos_dims):
      raise ValueError(f"zero-size array to reduction operation {name} which has no identity")

  result_dtype = dtype or dtypes.dtype(a)

  if dtype is None and promote_integers:
    result_dtype = _promote_integer_dtype(result_dtype)

  result_dtype = dtypes.canonicalize_dtype(result_dtype)

  if upcast_f16_for_computation and dtypes.issubdtype(result_dtype, np.inexact):
    computation_dtype = _upcast_f16(result_dtype)
  else:
    computation_dtype = result_dtype
  a = lax.convert_element_type(a, computation_dtype)
  op = op if computation_dtype != np.bool_ else bool_op
  # NB: in XLA, init_val must be an identity for the op, so the user-specified
  # initial value must be applied afterward.
  init_val = _reduction_init_val(a, init_val)
  if where_ is not None:
    a = _where(where_, a, init_val)
  if pos_dims is not dims:
    if parallel_reduce is None:
      raise NotImplementedError(f"Named reductions not implemented for jnp.{name}()")
    result = parallel_reduce(a, dims)
  else:
    result = lax.reduce(a, init_val, op, dims)
  if initial is not None:
    initial_arr = lax.convert_element_type(initial, lax_internal.asarray(a).dtype)
    if initial_arr.shape != ():
      raise ValueError("initial value must be a scalar. "
                       f"Got array of shape {initial_arr.shape}")
    result = op(initial_arr, result)
  if keepdims:
    result = lax.expand_dims(result, pos_dims)
  return lax.convert_element_type(result, dtype or result_dtype)

def _canonicalize_axis_allow_named(x, rank):
  return maybe_named_axis(x, lambda i: _canonicalize_axis(i, rank), lambda name: name)

def _reduction_dims(a: ArrayLike, axis: Axis):
  if axis is None:
    return (tuple(range(np.ndim(a))),) * 2
  elif not isinstance(axis, (np.ndarray, tuple, list)):
    axis = (axis,)  # type: ignore[assignment]
  canon_axis = tuple(_canonicalize_axis_allow_named(x, np.ndim(a))
                     for x in axis)  # type: ignore[union-attr]
  if len(canon_axis) != len(set(canon_axis)):
    raise ValueError(f"duplicate value in 'axis': {axis}")
  canon_pos_axis = tuple(x for x in canon_axis if isinstance(x, int))
  if len(canon_pos_axis) != len(canon_axis):
    return canon_pos_axis, canon_axis
  else:
    return canon_axis, canon_axis

def _reduction_init_val(a: ArrayLike, init_val: Any) -> np.ndarray:
  # This function uses np.* functions because lax pattern matches against the
  # specific concrete values of the reduction inputs.
  a_dtype = dtypes.canonicalize_dtype(dtypes.dtype(a))
  if a_dtype == 'bool':
    return np.array(init_val > 0, dtype=a_dtype)
  if (np.isinf(init_val) and dtypes.issubdtype(a_dtype, np.floating)
      and not dtypes.supports_inf(a_dtype)):
    init_val = np.array(dtypes.finfo(a_dtype).min if np.isneginf(init_val)
                        else dtypes.finfo(a_dtype).max, dtype=a_dtype)
  try:
    return np.array(init_val, dtype=a_dtype)
  except OverflowError:
    assert dtypes.issubdtype(a_dtype, np.integer)
    sign, info = np.sign(init_val), dtypes.iinfo(a_dtype)
    return np.array(info.min if sign < 0 else info.max, dtype=a_dtype)

def _cast_to_bool(operand: ArrayLike) -> Array:
  with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=NumpyComplexWarning)
    return lax.convert_element_type(operand, np.bool_)

def _cast_to_numeric(operand: ArrayLike) -> Array:
  return promote_dtypes_numeric(operand)[0]


def _ensure_optional_axes(x: Axis) -> Axis:
  def force(x):
    if x is None:
      return None
    try:
      return operator.index(x)
    except TypeError:
      return tuple(i if isinstance(i, str) else operator.index(i) for i in x)
  return core.concrete_or_error(
    force, x, "The axis argument must be known statically.")


@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims', 'promote_integers'), inline=True)
def _reduce_sum(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
                out: None = None, keepdims: bool = False,
                initial: ArrayLike | None = None, where: ArrayLike | None = None,
                promote_integers: bool = True) -> Array:
  return _reduction(a, "sum", np.sum, lax.add, 0, preproc=_cast_to_numeric,
                    bool_op=lax.bitwise_or, upcast_f16_for_computation=True,
                    axis=axis, dtype=dtype, out=out, keepdims=keepdims,
                    initial=initial, where_=where, parallel_reduce=lax.psum,
                    promote_integers=promote_integers)


def sum(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
        out: None = None, keepdims: bool = False, initial: ArrayLike | None = None,
        where: ArrayLike | None = None, promote_integers: bool = True) -> Array:
  r"""Sum of the elements of the array over a given axis.

  JAX implementation of :func:`numpy.sum`.

  Args:
    a: Input array.
    axis: int or array, default=None. Axis along which the sum to be computed.
      If None, the sum is computed along all the axes.
    dtype: The type of the output array. Default=None.
    out: Unused by JAX
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    initial: int or array, Default=None. Initial value for the sum.
    where: int or array, default=None. The elements to be used in the sum. Array
      should be broadcast compatible to the input.
    promote_integers : bool, default=True. If True, then integer inputs will be
      promoted to the widest available integer dtype, following numpy's behavior.
      If False, the result will have the same dtype as the input.
      ``promote_integers`` is ignored if ``dtype`` is specified.

  Returns:
    An array of the sum along the given axis.

  See also:
    - :func:`jax.numpy.prod`: Compute the product of array elements over a given
      axis.
    - :func:`jax.numpy.max`: Compute the maximum of array elements over given axis.
    - :func:`jax.numpy.min`: Compute the minimum of array elements over given axis.

  Examples:

    By default, the sum is computed along all the axes.

    >>> x = jnp.array([[1, 3, 4, 2],
    ...                [5, 2, 6, 3],
    ...                [8, 1, 3, 9]])
    >>> jnp.sum(x)
    Array(47, dtype=int32)

    If ``axis=1``, the sum is computed along axis 1.

    >>> jnp.sum(x, axis=1)
    Array([10, 16, 21], dtype=int32)

    If ``keepdims=True``, ``ndim`` of the output is equal to that of the input.

    >>> jnp.sum(x, axis=1, keepdims=True)
    Array([[10],
           [16],
           [21]], dtype=int32)

    To include only specific elements in the sum, you can use ``where``.

    >>> where=jnp.array([[0, 0, 1, 0],
    ...                  [0, 0, 1, 1],
    ...                  [1, 1, 1, 0]], dtype=bool)
    >>> jnp.sum(x, axis=1, keepdims=True, where=where)
    Array([[ 4],
           [ 9],
           [12]], dtype=int32)
    >>> where=jnp.array([[False],
    ...                  [False],
    ...                  [False]])
    >>> jnp.sum(x, axis=0, keepdims=True, where=where)
    Array([[0, 0, 0, 0]], dtype=int32)
  """
  return _reduce_sum(a, axis=_ensure_optional_axes(axis), dtype=dtype, out=out,
                     keepdims=keepdims, initial=initial, where=where,
                     promote_integers=promote_integers)


@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims', 'promote_integers'), inline=True)
def _reduce_prod(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
                 out: None = None, keepdims: bool = False,
                 initial: ArrayLike | None = None, where: ArrayLike | None = None,
                 promote_integers: bool = True) -> Array:
  return _reduction(a, "prod", np.prod, lax.mul, 1, preproc=_cast_to_numeric,
                    bool_op=lax.bitwise_and, upcast_f16_for_computation=True,
                    axis=axis, dtype=dtype, out=out, keepdims=keepdims,
                    initial=initial, where_=where, promote_integers=promote_integers)


def prod(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
         out: None = None, keepdims: bool = False,
         initial: ArrayLike | None = None, where: ArrayLike | None = None,
         promote_integers: bool = True) -> Array:
  r"""Return product of the array elements over a given axis.

  JAX implementation of :func:`numpy.prod`.

  Args:
    a: Input array.
    axis: int or array, default=None. Axis along which the product to be computed.
      If None, the product is computed along all the axes.
    dtype: The type of the output array. Default=None.
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    initial: int or array, Default=None. Initial value for the product.
    where: int or array, default=None. The elements to be used in the product.
      Array should be broadcast compatible to the input.
    promote_integers : bool, default=True. If True, then integer inputs will be
      promoted to the widest available integer dtype, following numpy's behavior.
      If False, the result will have the same dtype as the input.
      ``promote_integers`` is ignored if ``dtype`` is specified.
    out: Unused by JAX.

  Returns:
    An array of the product along the given axis.

  See also:
    - :func:`jax.numpy.sum`: Compute the sum of array elements over a given axis.
    - :func:`jax.numpy.max`: Compute the maximum of array elements over given axis.
    - :func:`jax.numpy.min`: Compute the minimum of array elements over given axis.

  Examples:
    By default, ``jnp.prod`` computes along all the axes.

    >>> x = jnp.array([[1, 3, 4, 2],
    ...                [5, 2, 1, 3],
    ...                [2, 1, 3, 1]])
    >>> jnp.prod(x)
    Array(4320, dtype=int32)

    If ``axis=1``, product is computed along axis 1.

    >>> jnp.prod(x, axis=1)
    Array([24, 30,  6], dtype=int32)

    If ``keepdims=True``, ``ndim`` of the output is equal to that of the input.

    >>> jnp.prod(x, axis=1, keepdims=True)
    Array([[24],
           [30],
           [ 6]], dtype=int32)

    To include only specific elements in the sum, you can use a``where``.

    >>> where=jnp.array([[1, 0, 1, 0],
    ...                  [0, 0, 1, 1],
    ...                  [1, 1, 1, 0]], dtype=bool)
    >>> jnp.prod(x, axis=1, keepdims=True, where=where)
    Array([[4],
           [3],
           [6]], dtype=int32)
    >>> where = jnp.array([[False],
    ...                    [False],
    ...                    [False]])
    >>> jnp.prod(x, axis=1, keepdims=True, where=where)
    Array([[1],
           [1],
           [1]], dtype=int32)
  """
  return _reduce_prod(a, axis=_ensure_optional_axes(axis), dtype=dtype,
                      out=out, keepdims=keepdims, initial=initial, where=where,
                      promote_integers=promote_integers)


@partial(api.jit, static_argnames=('axis', 'keepdims'), inline=True)
def _reduce_max(a: ArrayLike, axis: Axis = None, out: None = None,
                keepdims: bool = False, initial: ArrayLike | None = None,
                where: ArrayLike | None = None) -> Array:
  return _reduction(a, "max", np.max, lax.max, -np.inf, has_identity=False,
                    axis=axis, out=out, keepdims=keepdims,
                    initial=initial, where_=where, parallel_reduce=lax.pmax)


def max(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False, initial: ArrayLike | None = None,
        where: ArrayLike | None = None) -> Array:
  r"""Return the maximum of the array elements along a given axis.

  JAX implementation of :func:`numpy.max`.

  Args:
    a: Input array.
    axis: int or array, default=None. Axis along which the maximum to be computed.
      If None, the maximum is computed along all the axes.
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    initial: int or array, default=None. Initial value for the maximum.
    where: int or array of boolean dtype, default=None. The elements to be used
      in the maximum. Array should be broadcast compatible to the input.
      ``initial`` must be specified when ``where`` is used.
    out: Unused by JAX.

  Returns:
    An array of maximum values along the given axis.

  See also:
    - :func:`jax.numpy.min`: Compute the minimum of array elements along a given
      axis.
    - :func:`jax.numpy.sum`: Compute the sum of array elements along a given axis.
    - :func:`jax.numpy.prod`: Compute the product of array elements along a given
      axis.

  Examples:

    By default, ``jnp.max`` computes the maximum of elements along all the axes.

    >>> x = jnp.array([[9, 3, 4, 5],
    ...                [5, 2, 7, 4],
    ...                [8, 1, 3, 6]])
    >>> jnp.max(x)
    Array(9, dtype=int32)

    If ``axis=1``, the maximum will be computed along axis 1.

    >>> jnp.max(x, axis=1)
    Array([9, 7, 8], dtype=int32)

    If ``keepdims=True``, ``ndim`` of the output will be same of that of the input.

    >>> jnp.max(x, axis=1, keepdims=True)
    Array([[9],
           [7],
           [8]], dtype=int32)

    To include only specific elements in computing the maximum, you can use
    ``where``. It can either have same dimension as input

    >>> where=jnp.array([[0, 0, 1, 0],
    ...                  [0, 0, 1, 1],
    ...                  [1, 1, 1, 0]], dtype=bool)
    >>> jnp.max(x, axis=1, keepdims=True, initial=0, where=where)
    Array([[4],
           [7],
           [8]], dtype=int32)

    or must be broadcast compatible with input.

    >>> where = jnp.array([[False],
    ...                    [False],
    ...                    [False]])
    >>> jnp.max(x, axis=0, keepdims=True, initial=0, where=where)
    Array([[0, 0, 0, 0]], dtype=int32)
  """
  return _reduce_max(a, axis=_ensure_optional_axes(axis), out=out,
                     keepdims=keepdims, initial=initial, where=where)

@partial(api.jit, static_argnames=('axis', 'keepdims'), inline=True)
def _reduce_min(a: ArrayLike, axis: Axis = None, out: None = None,
                keepdims: bool = False, initial: ArrayLike | None = None,
                where: ArrayLike | None = None) -> Array:
  return _reduction(a, "min", np.min, lax.min, np.inf, has_identity=False,
                    axis=axis, out=out, keepdims=keepdims,
                    initial=initial, where_=where, parallel_reduce=lax.pmin)


def min(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False, initial: ArrayLike | None = None,
        where: ArrayLike | None = None) -> Array:
  r"""Return the minimum of array elements along a given axis.

  JAX implementation of :func:`numpy.min`.

  Args:
    a: Input array.
    axis: int or array, default=None. Axis along which the minimum to be computed.
      If None, the minimum is computed along all the axes.
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    initial: int or array, Default=None. Initial value for the minimum.
    where: int or array, default=None. The elements to be used in the minimum.
      Array should be broadcast compatible to the input. ``initial`` must be
      specified when ``where`` is used.
    out: Unused by JAX.

  Returns:
    An array of minimum values along the given axis.

  See also:
    - :func:`jax.numpy.max`: Compute the maximum of array elements along a given
      axis.
    - :func:`jax.numpy.sum`: Compute the sum of array elements along a given axis.
    - :func:`jax.numpy.prod`: Compute the product of array elements along a given
      axis.

  Examples:
    By default, the minimum is computed along all the axes.

    >>> x = jnp.array([[2, 5, 1, 6],
    ...                [3, -7, -2, 4],
    ...                [8, -4, 1, -3]])
    >>> jnp.min(x)
    Array(-7, dtype=int32)

    If ``axis=1``, the minimum is computed along axis 1.

    >>> jnp.min(x, axis=1)
    Array([ 1, -7, -4], dtype=int32)

    If ``keepdims=True``, ``ndim`` of the output will be same of that of the input.

    >>> jnp.min(x, axis=1, keepdims=True)
    Array([[ 1],
           [-7],
           [-4]], dtype=int32)

    To include only specific elements in computing the minimum, you can use
    ``where``. ``where`` can either have same dimension as input.

    >>> where=jnp.array([[1, 0, 1, 0],
    ...                  [0, 0, 1, 1],
    ...                  [1, 1, 1, 0]], dtype=bool)
    >>> jnp.min(x, axis=1, keepdims=True, initial=0, where=where)
    Array([[ 0],
           [-2],
           [-4]], dtype=int32)

    or must be broadcast compatible with input.

    >>> where = jnp.array([[False],
    ...                    [False],
    ...                    [False]])
    >>> jnp.min(x, axis=0, keepdims=True, initial=0, where=where)
    Array([[0, 0, 0, 0]], dtype=int32)
  """
  return _reduce_min(a, axis=_ensure_optional_axes(axis), out=out,
                     keepdims=keepdims, initial=initial, where=where)

@partial(api.jit, static_argnames=('axis', 'keepdims'), inline=True)
def _reduce_all(a: ArrayLike, axis: Axis = None, out: None = None,
                keepdims: bool = False, *, where: ArrayLike | None = None) -> Array:
  return _reduction(a, "all", np.all, lax.bitwise_and, True, preproc=_cast_to_bool,
                    axis=axis, out=out, keepdims=keepdims, where_=where)


def all(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False, *, where: ArrayLike | None = None) -> Array:
  r"""Test whether all array elements along a given axis evaluate to True.

  JAX implementation of :func:`numpy.all`.

  Args:
    a: Input array.
    axis: int or array, default=None. Axis along which to be tested. If None,
      tests along all the axes.
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    where: int or array of boolean dtype, default=None. The elements to be used
      in the test. Array should be broadcast compatible to the input.
    out: Unused by JAX.

  Returns:
    An array of boolean values.

  Examples:
    By default, ``jnp.all`` tests for True values along all the axes.

    >>> x = jnp.array([[True, True, True, False],
    ...                [True, False, True, False],
    ...                [True, True, False, False]])
    >>> jnp.all(x)
    Array(False, dtype=bool)

    If ``axis=0``, tests for True values along axis 0.

    >>> jnp.all(x, axis=0)
    Array([ True, False, False, False], dtype=bool)

    If ``keepdims=True``, ``ndim`` of the output will be same of that of the input.

    >>> jnp.all(x, axis=0, keepdims=True)
    Array([[ True, False, False, False]], dtype=bool)

    To include specific elements in testing for True values, you can use a``where``.

    >>> where=jnp.array([[1, 0, 1, 0],
    ...                  [0, 0, 1, 1],
    ...                  [1, 1, 1, 0]], dtype=bool)
    >>> jnp.all(x, axis=0, keepdims=True, where=where)
    Array([[ True,  True, False, False]], dtype=bool)
  """
  return _reduce_all(a, axis=_ensure_optional_axes(axis), out=out,
                     keepdims=keepdims, where=where)

@partial(api.jit, static_argnames=('axis', 'keepdims'), inline=True)
def _reduce_any(a: ArrayLike, axis: Axis = None, out: None = None,
                keepdims: bool = False, *, where: ArrayLike | None = None) -> Array:
  return _reduction(a, "any", np.any, lax.bitwise_or, False, preproc=_cast_to_bool,
                    axis=axis, out=out, keepdims=keepdims, where_=where)


def any(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False, *, where: ArrayLike | None = None) -> Array:
  r"""Test whether any of the array elements along a given axis evaluate to True.

  JAX implementation of :func:`numpy.any`.

  Args:
    a: Input array.
    axis: int or array, default=None. Axis along which to be tested. If None,
      tests along all the axes.
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    where: int or array of boolean dtype, default=None. The elements to be used
      in the test. Array should be broadcast compatible to the input.
    out: Unused by JAX.

  Returns:
    An array of boolean values.

  Examples:
    By default, ``jnp.any`` tests along all the axes.

    >>> x = jnp.array([[True, True, True, False],
    ...                [True, False, True, False],
    ...                [True, True, False, False]])
    >>> jnp.any(x)
    Array(True, dtype=bool)

    If ``axis=0``, tests along axis 0.

    >>> jnp.any(x, axis=0)
    Array([ True,  True,  True, False], dtype=bool)

    If ``keepdims=True``, ``ndim`` of the output will be same of that of the input.

    >>> jnp.any(x, axis=0, keepdims=True)
    Array([[ True,  True,  True, False]], dtype=bool)

    To include specific elements in testing for True values, you can use a``where``.

    >>> where=jnp.array([[1, 0, 1, 0],
    ...                  [0, 1, 0, 1],
    ...                  [1, 0, 1, 0]], dtype=bool)
    >>> jnp.any(x, axis=0, keepdims=True, where=where)
    Array([[ True, False,  True, False]], dtype=bool)
  """
  return _reduce_any(a, axis=_ensure_optional_axes(axis), out=out,
                     keepdims=keepdims, where=where)

def amin(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False, initial: ArrayLike | None = None,
        where: ArrayLike | None = None) -> Array:
  """Alias of :func:`jax.numpy.min`."""
  return min(a, axis=axis, out=out, keepdims=keepdims,
             initial=initial, where=where)

def amax(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False, initial: ArrayLike | None = None,
        where: ArrayLike | None = None) -> Array:
  """Alias of :func:`jax.numpy.max`."""
  return max(a, axis=axis, out=out, keepdims=keepdims,
             initial=initial, where=where)

def _axis_size(a: ArrayLike, axis: int | Sequence[int]):
  if not isinstance(axis, (tuple, list)):
    axis_seq: Sequence[int] = (axis,)  # type: ignore[assignment]
  else:
    axis_seq = axis
  size = 1
  a_shape = np.shape(a)
  for a in axis_seq:
    size *= maybe_named_axis(a, lambda i: a_shape[i], lambda name: lax.psum(1, name))
  return size


def mean(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
         out: None = None, keepdims: bool = False, *,
         where: ArrayLike | None = None) -> Array:
  r"""Return the mean of array elements along a given axis.

  JAX implementation of :func:`numpy.mean`.

  Args:
    a: input array.
    axis: optional, int or sequence of ints, default=None. Axis along which the
      mean to be computed. If None, mean is computed along all the axes.
    dtype: The type of the output array. Default=None.
    keepdims: bool, default=False. If true, reduced axes are left in the result
      with size 1.
    where: optional, boolean array, default=None. The elements to be used in the
      mean. Array should be broadcast compatible to the input.
    out: Unused by JAX.

  Returns:
    An array of the mean along the given axis.

  See also:
    - :func:`jax.numpy.sum`: Compute the sum of array elements over a given axis.
    - :func:`jax.numpy.max`: Compute the maximum of array elements over given axis.
    - :func:`jax.numpy.min`: Compute the minimum of array elements over given axis.

  Examples:
    By default, the mean is computed along all the axes.

    >>> x = jnp.array([[1, 3, 4, 2],
    ...                [5, 2, 6, 3],
    ...                [8, 1, 2, 9]])
    >>> jnp.mean(x)
    Array(3.8333335, dtype=float32)

    If ``axis=1``, the mean is computed along axis 1.

    >>> jnp.mean(x, axis=1)
    Array([2.5, 4. , 5. ], dtype=float32)

    If ``keepdims=True``, ``ndim`` of the output is equal to that of the input.

    >>> jnp.mean(x, axis=1, keepdims=True)
    Array([[2.5],
           [4. ],
           [5. ]], dtype=float32)

    To use only specific elements of ``x`` to compute the mean, you can use
    ``where``.

    >>> where = jnp.array([[1, 0, 1, 0],
    ...                    [0, 1, 0, 1],
    ...                    [1, 1, 0, 1]], dtype=bool)
    >>> jnp.mean(x, axis=1, keepdims=True, where=where)
    Array([[2.5],
           [2.5],
           [6. ]], dtype=float32)
  """
  return _mean(a, _ensure_optional_axes(axis), dtype, out, keepdims,
               where=where)

@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'), inline=True)
def _mean(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
          out: None = None, keepdims: bool = False, *,
          upcast_f16_for_computation: bool = True,
          where: ArrayLike | None = None) -> Array:
  check_arraylike("mean", a)
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.mean is not supported.")

  if dtype is None:
    result_dtype = dtypes.to_inexact_dtype(dtypes.dtype(a, canonicalize=True))
  else:
    dtypes.check_user_dtype_supported(dtype, "mean")
    result_dtype = dtypes.canonicalize_dtype(dtype)

  if upcast_f16_for_computation and dtypes.issubdtype(result_dtype, np.inexact):
    computation_dtype = _upcast_f16(result_dtype)
  else:
    computation_dtype = result_dtype

  if where is None:
    if axis is None:
      normalizer = core.dimension_as_value(np.size(a))
    else:
      normalizer = core.dimension_as_value(_axis_size(a, axis))
  else:
    normalizer = sum(_broadcast_to(where, np.shape(a)), axis, dtype=dtype, keepdims=keepdims)

  return lax.div(
      sum(a, axis, dtype=computation_dtype, keepdims=keepdims, where=where),
      lax.convert_element_type(normalizer, computation_dtype)
  ).astype(result_dtype)

@overload
def average(a: ArrayLike, axis: Axis = None, weights: ArrayLike | None = None,
            returned: Literal[False] = False, keepdims: bool = False) -> Array: ...
@overload
def average(a: ArrayLike, axis: Axis = None, weights: ArrayLike | None = None, *,
            returned: Literal[True], keepdims: bool = False) -> Array: ...
@overload
def average(a: ArrayLike, axis: Axis = None, weights: ArrayLike | None = None,
            returned: bool = False, keepdims: bool = False) -> Array | tuple[Array, Array]: ...
@implements(np.average)
def average(a: ArrayLike, axis: Axis = None, weights: ArrayLike | None = None,
            returned: bool = False, keepdims: bool = False) -> Array | tuple[Array, Array]:
  return _average(a, _ensure_optional_axes(axis), weights, returned, keepdims)

@partial(api.jit, static_argnames=('axis', 'returned', 'keepdims'), inline=True)
def _average(a: ArrayLike, axis: Axis = None, weights: ArrayLike | None = None,
             returned: bool = False, keepdims: bool = False) -> Array | tuple[Array, Array]:
  if weights is None: # Treat all weights as 1
    check_arraylike("average", a)
    a, = promote_dtypes_inexact(a)
    avg = mean(a, axis=axis, keepdims=keepdims)
    if axis is None:
      weights_sum = lax.full((), core.dimension_as_value(a.size), dtype=avg.dtype)
    elif isinstance(axis, tuple):
      weights_sum = lax.full_like(avg, math.prod(core.dimension_as_value(a.shape[d]) for d in axis))
    else:
      weights_sum = lax.full_like(avg, core.dimension_as_value(a.shape[axis]))  # type: ignore[index]
  else:
    check_arraylike("average", a, weights)
    a, weights = promote_dtypes_inexact(a, weights)

    a_shape = np.shape(a)
    a_ndim = len(a_shape)
    weights_shape = np.shape(weights)

    if axis is None:
      pass
    elif isinstance(axis, tuple):
      axis = tuple(_canonicalize_axis(d, a_ndim) for d in axis)
    else:
      axis = _canonicalize_axis(axis, a_ndim)

    if a_shape != weights_shape:
      # Make sure the dimensions work out
      if len(weights_shape) != 1:
        raise ValueError("1D weights expected when shapes of a and "
                         "weights differ.")
      if axis is None:
        raise ValueError("Axis must be specified when shapes of a and "
                         "weights differ.")
      elif isinstance(axis, tuple):
        raise ValueError("Single axis expected when shapes of a and weights differ")
      elif not core.definitely_equal(weights_shape[0], a_shape[axis]):
        raise ValueError("Length of weights not "
                         "compatible with specified axis.")

      weights = _broadcast_to(weights, (a_ndim - 1) * (1,) + weights_shape)
      weights = _moveaxis(weights, -1, axis)

    weights_sum = sum(weights, axis=axis, keepdims=keepdims)
    avg = sum(a * weights, axis=axis, keepdims=keepdims) / weights_sum

  if returned:
    if avg.shape != weights_sum.shape:
      weights_sum = _broadcast_to(weights_sum, avg.shape)
    return avg, weights_sum
  return avg


@implements(np.var, skip_params=['out'])
def var(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
        out: None = None, ddof: int = 0, keepdims: bool = False, *,
        where: ArrayLike | None = None, correction: int | float | None = None) -> Array:
  if correction is None:
    correction = ddof
  elif not isinstance(ddof, int) or ddof != 0:
    raise ValueError("ddof and correction can't be provided simultaneously.")
  return _var(a, _ensure_optional_axes(axis), dtype, out, correction, keepdims,
              where=where)

@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def _var(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
         out: None = None, correction: int | float = 0, keepdims: bool = False, *,
         where: ArrayLike | None = None) -> Array:
  check_arraylike("var", a)
  dtypes.check_user_dtype_supported(dtype, "var")
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.var is not supported.")

  computation_dtype, dtype = _var_promote_types(dtypes.dtype(a), dtype)
  a = lax_internal.asarray(a).astype(computation_dtype)
  a_mean = mean(a, axis, dtype=computation_dtype, keepdims=True, where=where)
  centered = lax.sub(a, a_mean)
  if dtypes.issubdtype(computation_dtype, np.complexfloating):
    centered = lax.real(lax.mul(centered, lax.conj(centered)))
    computation_dtype = centered.dtype  # avoid casting to complex below.
  else:
    centered = lax.square(centered)

  if where is None:
    if axis is None:
      normalizer = core.dimension_as_value(np.size(a))
    else:
      normalizer = core.dimension_as_value(_axis_size(a, axis))
    normalizer = lax.convert_element_type(normalizer, computation_dtype)
  else:
    normalizer = sum(_broadcast_to(where, np.shape(a)), axis,
                     dtype=computation_dtype, keepdims=keepdims)
  normalizer = lax.sub(normalizer, lax.convert_element_type(correction, computation_dtype))
  result = sum(centered, axis, dtype=computation_dtype, keepdims=keepdims, where=where)
  return _where(normalizer > 0, lax.div(result, normalizer).astype(dtype), np.nan)


def _var_promote_types(a_dtype: DTypeLike, dtype: DTypeLike | None) -> tuple[DType, DType]:
  if dtype:
    if (not dtypes.issubdtype(dtype, np.complexfloating) and
        dtypes.issubdtype(a_dtype, np.complexfloating)):
      msg = ("jax.numpy.var does not yet support real dtype parameters when "
             "computing the variance of an array of complex values. The "
             "semantics of numpy.var seem unclear in this case. Please comment "
             "on https://github.com/google/jax/issues/2283 if this behavior is "
             "important to you.")
      raise ValueError(msg)
    computation_dtype = dtype
  else:
    if not dtypes.issubdtype(a_dtype, np.inexact):
      dtype = dtypes.to_inexact_dtype(a_dtype)
      computation_dtype = dtype
    else:
      dtype = _complex_elem_type(a_dtype)
      computation_dtype = a_dtype
  return _upcast_f16(computation_dtype), np.dtype(dtype)


@implements(np.std, skip_params=['out'])
def std(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
        out: None = None, ddof: int = 0, keepdims: bool = False, *,
        where: ArrayLike | None = None, correction: int | float | None = None) -> Array:
  if correction is None:
    correction = ddof
  elif not isinstance(ddof, int) or ddof != 0:
    raise ValueError("ddof and correction can't be provided simultaneously.")
  return _std(a, _ensure_optional_axes(axis), dtype, out, correction, keepdims,
              where=where)

@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def _std(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None,
         out: None = None, correction: int | float = 0, keepdims: bool = False, *,
         where: ArrayLike | None = None) -> Array:
  check_arraylike("std", a)
  dtypes.check_user_dtype_supported(dtype, "std")
  if dtype is not None and not dtypes.issubdtype(dtype, np.inexact):
    raise ValueError(f"dtype argument to jnp.std must be inexact; got {dtype}")
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.std is not supported.")
  return lax.sqrt(var(a, axis=axis, dtype=dtype, correction=correction, keepdims=keepdims, where=where))


@implements(np.ptp, skip_params=['out'])
def ptp(a: ArrayLike, axis: Axis = None, out: None = None,
        keepdims: bool = False) -> Array:
  return _ptp(a, _ensure_optional_axes(axis), out, keepdims)

@partial(api.jit, static_argnames=('axis', 'keepdims'))
def _ptp(a: ArrayLike, axis: Axis = None, out: None = None,
         keepdims: bool = False) -> Array:
  check_arraylike("ptp", a)
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.ptp is not supported.")
  x = amax(a, axis=axis, keepdims=keepdims)
  y = amin(a, axis=axis, keepdims=keepdims)
  return lax.sub(x, y)


@implements(np.count_nonzero)
@partial(api.jit, static_argnames=('axis', 'keepdims'))
def count_nonzero(a: ArrayLike, axis: Axis = None,
                  keepdims: bool = False) -> Array:
  check_arraylike("count_nonzero", a)
  return sum(lax.ne(a, _lax_const(a, 0)), axis=axis,
             dtype=dtypes.canonicalize_dtype(int), keepdims=keepdims)


def _nan_reduction(a: ArrayLike, name: str, jnp_reduction: Callable[..., Array],
                   init_val: ArrayLike, nan_if_all_nan: bool,
                   axis: Axis = None, keepdims: bool = False, **kwargs) -> Array:
  check_arraylike(name, a)
  if not dtypes.issubdtype(dtypes.dtype(a), np.inexact):
    return jnp_reduction(a, axis=axis, keepdims=keepdims, **kwargs)

  out = jnp_reduction(_where(lax_internal._isnan(a), _reduction_init_val(a, init_val), a),
                      axis=axis, keepdims=keepdims, **kwargs)
  if nan_if_all_nan:
    return _where(all(lax_internal._isnan(a), axis=axis, keepdims=keepdims),
                  _lax_const(a, np.nan), out)
  else:
    return out

@implements(np.nanmin, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'keepdims'))
def nanmin(a: ArrayLike, axis: Axis = None, out: None = None,
           keepdims: bool = False, initial: ArrayLike | None = None,
           where: ArrayLike | None = None) -> Array:
  return _nan_reduction(a, 'nanmin', min, np.inf, nan_if_all_nan=initial is None,
                        axis=axis, out=out, keepdims=keepdims,
                        initial=initial, where=where)

@implements(np.nanmax, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'keepdims'))
def nanmax(a: ArrayLike, axis: Axis = None, out: None = None,
           keepdims: bool = False, initial: ArrayLike | None = None,
           where: ArrayLike | None = None) -> Array:
  return _nan_reduction(a, 'nanmax', max, -np.inf, nan_if_all_nan=initial is None,
                        axis=axis, out=out, keepdims=keepdims,
                        initial=initial, where=where)

@implements(np.nansum, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def nansum(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None, out: None = None,
           keepdims: bool = False, initial: ArrayLike | None = None,
           where: ArrayLike | None = None) -> Array:
  dtypes.check_user_dtype_supported(dtype, "nanprod")
  return _nan_reduction(a, 'nansum', sum, 0, nan_if_all_nan=False,
                        axis=axis, dtype=dtype, out=out, keepdims=keepdims,
                        initial=initial, where=where)

# Work around a sphinx documentation warning in NumPy 1.22.
if nansum.__doc__ is not None:
  nansum.__doc__ = nansum.__doc__.replace("\n\n\n", "\n\n")

@implements(np.nanprod, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def nanprod(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None, out: None = None,
            keepdims: bool = False, initial: ArrayLike | None = None,
            where: ArrayLike | None = None) -> Array:
  dtypes.check_user_dtype_supported(dtype, "nanprod")
  return _nan_reduction(a, 'nanprod', prod, 1, nan_if_all_nan=False,
                        axis=axis, dtype=dtype, out=out, keepdims=keepdims,
                        initial=initial, where=where)

@implements(np.nanmean, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def nanmean(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None, out: None = None,
            keepdims: bool = False, where: ArrayLike | None = None) -> Array:
  check_arraylike("nanmean", a)
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.nanmean is not supported.")
  if dtypes.issubdtype(dtypes.dtype(a), np.bool_) or dtypes.issubdtype(dtypes.dtype(a), np.integer):
    return mean(a, axis, dtype, out, keepdims, where=where)
  if dtype is None:
    dtype = dtypes.to_inexact_dtype(dtypes.dtype(a, canonicalize=True))
  else:
    dtypes.check_user_dtype_supported(dtype, "mean")
    dtype = dtypes.canonicalize_dtype(dtype)
  nan_mask = lax_internal.bitwise_not(lax_internal._isnan(a))
  normalizer = sum(nan_mask, axis=axis, dtype=dtype, keepdims=keepdims, where=where)
  td = lax.div(nansum(a, axis, dtype=dtype, keepdims=keepdims, where=where), normalizer)
  return td


@implements(np.nanvar, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def nanvar(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None, out: None = None,
           ddof: int = 0, keepdims: bool = False,
           where: ArrayLike | None = None) -> Array:
  check_arraylike("nanvar", a)
  dtypes.check_user_dtype_supported(dtype, "nanvar")
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.nanvar is not supported.")

  computation_dtype, dtype = _var_promote_types(dtypes.dtype(a), dtype)
  a = lax_internal.asarray(a).astype(computation_dtype)
  a_mean = nanmean(a, axis, dtype=computation_dtype, keepdims=True, where=where)

  centered = _where(lax_internal._isnan(a), 0, lax.sub(a, a_mean))  # double-where trick for gradients.
  if dtypes.issubdtype(centered.dtype, np.complexfloating):
    centered = lax.real(lax.mul(centered, lax.conj(centered)))
  else:
    centered = lax.square(centered)

  normalizer = sum(lax_internal.bitwise_not(lax_internal._isnan(a)),
                   axis=axis, keepdims=keepdims, where=where)
  normalizer = normalizer - ddof
  normalizer_mask = lax.le(normalizer, lax_internal._zero(normalizer))
  result = sum(centered, axis, keepdims=keepdims, where=where)
  result = _where(normalizer_mask, np.nan, result)
  divisor = _where(normalizer_mask, 1, normalizer)
  result = lax.div(result, lax.convert_element_type(divisor, result.dtype))
  return lax.convert_element_type(result, dtype)


@implements(np.nanstd, skip_params=['out'])
@partial(api.jit, static_argnames=('axis', 'dtype', 'keepdims'))
def nanstd(a: ArrayLike, axis: Axis = None, dtype: DTypeLike | None = None, out: None = None,
           ddof: int = 0, keepdims: bool = False,
           where: ArrayLike | None = None) -> Array:
  check_arraylike("nanstd", a)
  dtypes.check_user_dtype_supported(dtype, "nanstd")
  if out is not None:
    raise NotImplementedError("The 'out' argument to jnp.nanstd is not supported.")
  return lax.sqrt(nanvar(a, axis=axis, dtype=dtype, ddof=ddof, keepdims=keepdims, where=where))


class CumulativeReduction(Protocol):
  def __call__(self, a: ArrayLike, axis: Axis = None,
               dtype: DTypeLike | None = None, out: None = None) -> Array: ...


# TODO(jakevdp): should we change these semantics to match those of numpy?
CUML_REDUCTION_LAX_DESCRIPTION = """
Unlike the numpy counterpart, when ``dtype`` is not specified the output dtype will always
match the dtype of the input.
"""

def _make_cumulative_reduction(np_reduction: Any, reduction: Callable[..., Array],
                               fill_nan: bool = False, fill_value: ArrayLike = 0,
                               promote_integers: bool = False) -> CumulativeReduction:
  @implements(np_reduction, skip_params=['out'],
          lax_description=CUML_REDUCTION_LAX_DESCRIPTION)
  def cumulative_reduction(a: ArrayLike, axis: Axis = None,
                           dtype: DTypeLike | None = None, out: None = None) -> Array:
    return _cumulative_reduction(a, _ensure_optional_axes(axis), dtype, out)

  @partial(api.jit, static_argnames=('axis', 'dtype'))
  def _cumulative_reduction(a: ArrayLike, axis: Axis = None,
                            dtype: DTypeLike | None = None, out: None = None) -> Array:
    check_arraylike(np_reduction.__name__, a)
    if out is not None:
      raise NotImplementedError(f"The 'out' argument to jnp.{np_reduction.__name__} "
                                f"is not supported.")
    dtypes.check_user_dtype_supported(dtype, np_reduction.__name__)

    if axis is None or _isscalar(a):
      a = lax.reshape(a, (np.size(a),))
    if axis is None:
      axis = 0

    a_shape = list(np.shape(a))
    num_dims = len(a_shape)
    axis = _canonicalize_axis(axis, num_dims)

    if fill_nan:
      a = _where(lax_internal._isnan(a), _lax_const(a, fill_value), a)

    result_type: DTypeLike = dtypes.dtype(dtype or a)
    if dtype is None and promote_integers or dtypes.issubdtype(result_type, np.bool_):
      result_type = _promote_integer_dtype(result_type)
    result_type = dtypes.canonicalize_dtype(result_type)

    a = lax.convert_element_type(a, result_type)
    result = reduction(a, axis)

    # We downcast to boolean because we accumulate in integer types
    if dtypes.issubdtype(dtype, np.bool_):
      result = lax.convert_element_type(result, np.bool_)
    return result

  return cumulative_reduction


cumsum = _make_cumulative_reduction(np.cumsum, lax.cumsum, fill_nan=False)
cumprod = _make_cumulative_reduction(np.cumprod, lax.cumprod, fill_nan=False)
nancumsum = _make_cumulative_reduction(np.nancumsum, lax.cumsum,
                                       fill_nan=True, fill_value=0)
nancumprod = _make_cumulative_reduction(np.nancumprod, lax.cumprod,
                                        fill_nan=True, fill_value=1)
_cumsum_with_promotion = _make_cumulative_reduction(
  np.cumsum, lax.cumsum, fill_nan=False, promote_integers=True
)

@implements(getattr(np, 'cumulative_sum', None))
def cumulative_sum(
    x: ArrayLike, /, *, axis: int | None = None,
    dtype: DTypeLike | None = None,
    include_initial: bool = False) -> Array:
  check_arraylike("cumulative_sum", x)
  x = lax_internal.asarray(x)
  if x.ndim == 0:
    raise ValueError(
      "The input must be non-scalar to take a cumulative sum, however a "
      "scalar value or scalar array was given."
    )
  if axis is None:
    axis = 0
    if x.ndim > 1:
      raise ValueError(
        f"The input array has rank {x.ndim}, however axis was not set to an "
        "explicit value. The axis argument is only optional for one-dimensional "
        "arrays.")

  axis = _canonicalize_axis(axis, x.ndim)
  dtypes.check_user_dtype_supported(dtype)
  out = _cumsum_with_promotion(x, axis=axis, dtype=dtype)
  if include_initial:
    zeros_shape = list(x.shape)
    zeros_shape[axis] = 1
    out = lax_internal.concatenate(
      [lax_internal.full(zeros_shape, 0, dtype=out.dtype), out],
      dimension=axis)
  return out

# Quantiles

# TODO(jakevdp): interpolation argument deprecated 2024-05-16
@implements(np.quantile, skip_params=['out', 'overwrite_input'])
@partial(api.jit, static_argnames=('axis', 'overwrite_input', 'interpolation', 'keepdims', 'method'))
def quantile(a: ArrayLike, q: ArrayLike, axis: int | tuple[int, ...] | None = None,
             out: None = None, overwrite_input: bool = False, method: str = "linear",
             keepdims: bool = False, *, interpolation: DeprecatedArg | str = DeprecatedArg()) -> Array:
  check_arraylike("quantile", a, q)
  if overwrite_input or out is not None:
    msg = ("jax.numpy.quantile does not support overwrite_input=True or "
           "out != None")
    raise ValueError(msg)
  if not isinstance(interpolation, DeprecatedArg):
    warnings.warn("The interpolation= argument to 'quantile' is deprecated. "
                  "Use 'method=' instead.", DeprecationWarning, stacklevel=2)
    method = interpolation
  return _quantile(lax_internal.asarray(a), lax_internal.asarray(q), axis, method, keepdims, False)

# TODO(jakevdp): interpolation argument deprecated 2024-05-16
@implements(np.nanquantile, skip_params=['out', 'overwrite_input'])
@partial(api.jit, static_argnames=('axis', 'overwrite_input', 'interpolation', 'keepdims', 'method'))
def nanquantile(a: ArrayLike, q: ArrayLike, axis: int | tuple[int, ...] | None = None,
                out: None = None, overwrite_input: bool = False, method: str = "linear",
                keepdims: bool = False, *, interpolation: DeprecatedArg | str = DeprecatedArg()) -> Array:
  check_arraylike("nanquantile", a, q)
  if overwrite_input or out is not None:
    msg = ("jax.numpy.nanquantile does not support overwrite_input=True or "
           "out != None")
    raise ValueError(msg)
  if not isinstance(interpolation, DeprecatedArg):
    warnings.warn("The interpolation= argument to 'nanquantile' is deprecated. "
                  "Use 'method=' instead.", DeprecationWarning, stacklevel=2)
    method = interpolation
  return _quantile(lax_internal.asarray(a), lax_internal.asarray(q), axis, method, keepdims, True)

def _quantile(a: Array, q: Array, axis: int | tuple[int, ...] | None,
              method: str, keepdims: bool, squash_nans: bool) -> Array:
  if method not in ["linear", "lower", "higher", "midpoint", "nearest"]:
    raise ValueError("method can only be 'linear', 'lower', 'higher', 'midpoint', or 'nearest'")
  a, = promote_dtypes_inexact(a)
  keepdim = []
  if dtypes.issubdtype(a.dtype, np.complexfloating):
    raise ValueError("quantile does not support complex input, as the operation is poorly defined.")
  if axis is None:
    if keepdims:
      keepdim = [1] * a.ndim
    a = a.ravel()
    axis = 0
  elif isinstance(axis, tuple):
    keepdim = list(a.shape)
    nd = a.ndim
    axis = tuple(_canonicalize_axis(ax, nd) for ax in axis)
    if len(set(axis)) != len(axis):
      raise ValueError('repeated axis')
    for ax in axis:
      keepdim[ax] = 1

    keep = set(range(nd)) - set(axis)
    # prepare permutation
    dimensions = list(range(nd))
    for i, s in enumerate(sorted(keep)):
      dimensions[i], dimensions[s] = dimensions[s], dimensions[i]
    do_not_touch_shape = tuple(x for idx,x in enumerate(a.shape) if idx not in axis)
    touch_shape = tuple(x for idx,x in enumerate(a.shape) if idx in axis)
    a = lax.reshape(a, do_not_touch_shape + (math.prod(touch_shape),), dimensions)
    axis = _canonicalize_axis(-1, a.ndim)
  else:
    axis = _canonicalize_axis(axis, a.ndim)

  q_shape = q.shape
  q_ndim = q.ndim
  if q_ndim > 1:
    raise ValueError(f"q must be have rank <= 1, got shape {q.shape}")

  a_shape = a.shape

  if squash_nans:
    a = _where(ufuncs.isnan(a), np.nan, a) # Ensure nans are positive so they sort to the end.
    a = lax.sort(a, dimension=axis)
    counts = sum(ufuncs.logical_not(ufuncs.isnan(a)), axis=axis, dtype=q.dtype, keepdims=keepdims)
    shape_after_reduction = counts.shape
    q = lax.expand_dims(
      q, tuple(range(q_ndim, len(shape_after_reduction) + q_ndim)))
    counts = lax.expand_dims(counts, tuple(range(q_ndim)))
    q = lax.mul(q, lax.sub(counts, _lax_const(q, 1)))
    low = lax.floor(q)
    high = lax.ceil(q)
    high_weight = lax.sub(q, low)
    low_weight = lax.sub(_lax_const(high_weight, 1), high_weight)

    low = lax.max(_lax_const(low, 0), lax.min(low, counts - 1))
    high = lax.max(_lax_const(high, 0), lax.min(high, counts - 1))
    low = lax.convert_element_type(low, int)
    high = lax.convert_element_type(high, int)
    out_shape = q_shape + shape_after_reduction
    index = [lax.broadcasted_iota(int, out_shape, dim + q_ndim)
             for dim in range(len(shape_after_reduction))]
    if keepdims:
      index[axis] = low
    else:
      index.insert(axis, low)
    low_value = a[tuple(index)]
    index[axis] = high
    high_value = a[tuple(index)]
  else:
    a = _where(any(ufuncs.isnan(a), axis=axis, keepdims=True), np.nan, a)
    a = lax.sort(a, dimension=axis)
    n = lax.convert_element_type(a_shape[axis], lax_internal._dtype(q))
    q = lax.mul(q, n - 1)
    low = lax.floor(q)
    high = lax.ceil(q)
    high_weight = lax.sub(q, low)
    low_weight = lax.sub(_lax_const(high_weight, 1), high_weight)

    low = lax.clamp(_lax_const(low, 0), low, n - 1)
    high = lax.clamp(_lax_const(high, 0), high, n - 1)
    low = lax.convert_element_type(low, int)
    high = lax.convert_element_type(high, int)

    slice_sizes = list(a_shape)
    slice_sizes[axis] = 1
    dnums = lax.GatherDimensionNumbers(
      offset_dims=tuple(range(
        q_ndim,
        len(a_shape) + q_ndim if keepdims else len(a_shape) + q_ndim - 1)),
      collapsed_slice_dims=() if keepdims else (axis,),
      start_index_map=(axis,))
    low_value = lax.gather(a, low[..., None], dimension_numbers=dnums,
                           slice_sizes=slice_sizes)
    high_value = lax.gather(a, high[..., None], dimension_numbers=dnums,
                            slice_sizes=slice_sizes)
    if q_ndim == 1:
      low_weight = lax.broadcast_in_dim(low_weight, low_value.shape,
                                        broadcast_dimensions=(0,))
      high_weight = lax.broadcast_in_dim(high_weight, high_value.shape,
                                        broadcast_dimensions=(0,))

  if method == "linear":
    result = lax.add(lax.mul(low_value.astype(q.dtype), low_weight),
                     lax.mul(high_value.astype(q.dtype), high_weight))
  elif method == "lower":
    result = low_value
  elif method == "higher":
    result = high_value
  elif method == "nearest":
    pred = lax.le(high_weight, _lax_const(high_weight, 0.5))
    result = lax.select(pred, low_value, high_value)
  elif method == "midpoint":
    result = lax.mul(lax.add(low_value, high_value), _lax_const(low_value, 0.5))
  else:
    raise ValueError(f"{method=!r} not recognized")
  if keepdims and keepdim:
    if q_ndim > 0:
      keepdim = [np.shape(q)[0], *keepdim]
    result = result.reshape(keepdim)
  return lax.convert_element_type(result, a.dtype)

# TODO(jakevdp): interpolation argument deprecated 2024-05-16
@implements(np.percentile, skip_params=['out', 'overwrite_input'])
@partial(api.jit, static_argnames=('axis', 'overwrite_input', 'interpolation', 'keepdims', 'method'))
def percentile(a: ArrayLike, q: ArrayLike,
               axis: int | tuple[int, ...] | None = None,
               out: None = None, overwrite_input: bool = False, method: str = "linear",
               keepdims: bool = False, *, interpolation: str | DeprecatedArg = DeprecatedArg()) -> Array:
  check_arraylike("percentile", a, q)
  q, = promote_dtypes_inexact(q)
  if not isinstance(interpolation, DeprecatedArg):
    warnings.warn("The interpolation= argument to 'percentile' is deprecated. "
                  "Use 'method=' instead.", DeprecationWarning, stacklevel=2)
    method = interpolation
  return quantile(a, q / 100, axis=axis, out=out, overwrite_input=overwrite_input,
                  method=method, keepdims=keepdims)

# TODO(jakevdp): interpolation argument deprecated 2024-05-16
@implements(np.nanpercentile, skip_params=['out', 'overwrite_input'])
@partial(api.jit, static_argnames=('axis', 'overwrite_input', 'interpolation', 'keepdims', 'method'))
def nanpercentile(a: ArrayLike, q: ArrayLike,
                  axis: int | tuple[int, ...] | None = None,
                  out: None = None, overwrite_input: bool = False, method: str = "linear",
                  keepdims: bool = False, *, interpolation: str | DeprecatedArg = DeprecatedArg()) -> Array:
  check_arraylike("nanpercentile", a, q)
  q = ufuncs.true_divide(q, 100.0)
  if not isinstance(interpolation, DeprecatedArg):
    warnings.warn("The interpolation= argument to 'nanpercentile' is deprecated. "
                  "Use 'method=' instead.", DeprecationWarning, stacklevel=2)
    method = interpolation
  return nanquantile(a, q, axis=axis, out=out, overwrite_input=overwrite_input,
                     method=method, keepdims=keepdims)

@implements(np.median, skip_params=['out', 'overwrite_input'])
@partial(api.jit, static_argnames=('axis', 'overwrite_input', 'keepdims'))
def median(a: ArrayLike, axis: int | tuple[int, ...] | None = None,
           out: None = None, overwrite_input: bool = False,
           keepdims: bool = False) -> Array:
  check_arraylike("median", a)
  return quantile(a, 0.5, axis=axis, out=out, overwrite_input=overwrite_input,
                  keepdims=keepdims, method='midpoint')

@implements(np.nanmedian, skip_params=['out', 'overwrite_input'])
@partial(api.jit, static_argnames=('axis', 'overwrite_input', 'keepdims'))
def nanmedian(a: ArrayLike, axis: int | tuple[int, ...] | None = None,
              out: None = None, overwrite_input: bool = False,
              keepdims: bool = False) -> Array:
  check_arraylike("nanmedian", a)
  return nanquantile(a, 0.5, axis=axis, out=out,
                     overwrite_input=overwrite_input, keepdims=keepdims,
                     method='midpoint')
