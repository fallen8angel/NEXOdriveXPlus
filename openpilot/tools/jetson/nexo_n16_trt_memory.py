"""Opt-in 8 GiB N16 experiment: lower TensorRT tactic memory, not live AI."""
from __future__ import annotations
from contextlib import contextmanager
import inspect

MIB = 1048576


def validate_tactic_dram_mib(value):
  if value is None:
    return None
  if type(value) is not int or value not in (512, 1024):
    raise ValueError('tactic_dram_mib must be 512 or 1024 MiB')
  return value * MIB


class _BoundedBuilder:
  def __init__(self, native, trt, limit):
    self._native, self._trt, self._limit = native, trt, limit
    if not hasattr(native, 'max_threads'):
      raise RuntimeError('TensorRT Builder.max_threads unavailable')
    native.max_threads = 1
    if native.max_threads != 1:
      raise RuntimeError('TensorRT did not accept single builder thread')

  def create_builder_config(self):
    config = self._native.create_builder_config()
    pool = getattr(getattr(self._trt, 'MemoryPoolType', None), 'TACTIC_DRAM', None)
    if pool is None:
      raise RuntimeError('TensorRT TACTIC_DRAM unavailable')
    if not callable(getattr(config, 'set_memory_pool_limit', None)) or not callable(getattr(config, 'get_memory_pool_limit', None)):
      raise RuntimeError('TensorRT memory pool API unavailable')
    config.set_memory_pool_limit(pool, self._limit)
    if config.get_memory_pool_limit(pool) != self._limit:
      raise RuntimeError('TensorRT rejected TACTIC_DRAM limit')
    if not hasattr(config, 'max_aux_streams'):
      raise RuntimeError('TensorRT max_aux_streams unavailable')
    config.max_aux_streams = 0
    if config.max_aux_streams != 0:
      raise RuntimeError('TensorRT rejected max_aux_streams=0')
    return config

  def __getattr__(self, name):
    return getattr(self._native, name)


class _TrtView:
  """Override only the pinned vendor module's TRT reference in this worker."""

  def __init__(self, module, limit):
    self._module, self._limit = module, limit

  def Builder(self, *args, **kwargs):
    return _BoundedBuilder(self._module.Builder(*args, **kwargs), self._module, self._limit)

  def __getattr__(self, name):
    return getattr(self._module, name)


@contextmanager
def bounded_vendor_builder(vendor, tactic_dram_mib):
  limit = validate_tactic_dram_mib(tactic_dram_mib)
  if limit is None:
    raise ValueError('Explicit tactic DRAM limit required')
  try:
    source = inspect.getsource(vendor.build_engine)
  except (OSError, TypeError) as e:
    raise RuntimeError('Pinned Carrot builder source unavailable') from e
  if not all(s in source for s in ('trt.Builder(', 'create_builder_config(', 'build_serialized_network(')):
    raise RuntimeError('Pinned Carrot builder contract changed')
  original = vendor.trt
  if getattr(original, '__version__', None) != '10.3.0':
    raise RuntimeError('Requires pinned TensorRT 10.3.0')
  vendor.trt = _TrtView(original, limit)
  try:
    yield
  finally:
    vendor.trt = original
