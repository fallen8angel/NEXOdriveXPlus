"""CPU-only bounded Carrot N16 builder isolation regression tests."""
from types import SimpleNamespace
import pytest
from openpilot.tools.jetson.nexo_n16_trt_memory import (
  MIB, bounded_vendor_builder, validate_tactic_dram_mib,
)


def test_profile_bounds():
  assert validate_tactic_dram_mib(None) is None
  assert validate_tactic_dram_mib(512) == 512*MIB
  assert validate_tactic_dram_mib(1024) == 1024*MIB
  for item in (-1, 0, 128, 256, 2048, True, '512', 512.0):
    with pytest.raises(ValueError):
      validate_tactic_dram_mib(item)


def fake_vendor(missing_pool=False, reject=False, version='10.3.0'):
  class Config:
    max_aux_streams = 4
    def __init__(self):
      self.pools = {}
    def set_memory_pool_limit(self, pool, size):
      if not reject:
        self.pools[pool] = size
    def get_memory_pool_limit(self, pool):
      return self.pools.get(pool, 0)
  class Builder:
    max_threads = 8
    def __init__(self, logger):
      self.config = None
    def create_network(self, flags):
      return 'network'
    def create_builder_config(self):
      self.config = Config()
      return self.config
    def build_serialized_network(self, net, cfg):
      return {'threads': self.max_threads, 'pools': cfg.pools, 'aux': cfg.max_aux_streams}
  trt = SimpleNamespace(__version__=version, Builder=Builder,
                        MemoryPoolType=SimpleNamespace(TACTIC_DRAM=None if missing_pool else 99))
  vendor = SimpleNamespace(trt=trt)
  def build_engine():
    trt = vendor.trt
    builder = trt.Builder(None)
    network = builder.create_network(0)
    config = builder.create_builder_config()
    return builder.build_serialized_network(network, config)
  vendor.build_engine = build_engine
  return vendor, trt


def test_native_carrot_builder_remains_unchanged_after_trial():
  vendor, original = fake_vendor()
  assert vendor.build_engine()['threads'] == 8
  with bounded_vendor_builder(vendor, 512):
    assert vendor.build_engine() == {'threads': 1, 'pools': {99: 512*MIB}, 'aux': 0}
  assert vendor.trt is original
  assert vendor.build_engine()['threads'] == 8


def test_rejected_pool_limit_restores_vendor():
  vendor, original = fake_vendor(reject=True)
  with pytest.raises(RuntimeError, match='rejected'):
    with bounded_vendor_builder(vendor, 512):
      vendor.build_engine()
  assert vendor.trt is original


def test_missing_tactic_dram_rejected():
  vendor, original = fake_vendor(missing_pool=True)
  with pytest.raises(RuntimeError, match='unavailable'):
    with bounded_vendor_builder(vendor, 1024):
      vendor.build_engine()
  assert vendor.trt is original


def test_unexpected_vendor_or_trt_version_rejected():
  vendor, original = fake_vendor(version='11.0.0')
  with pytest.raises(RuntimeError, match='10.3.0'):
    with bounded_vendor_builder(vendor, 512):
      pass
  assert vendor.trt is original
  vendor, original = fake_vendor()
  vendor.build_engine = lambda: None
  with pytest.raises(RuntimeError, match='source unavailable|contract changed'):
    with bounded_vendor_builder(vendor, 512):
      pass
  assert vendor.trt is original
