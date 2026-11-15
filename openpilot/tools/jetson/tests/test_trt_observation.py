"""Real pinned engine with fake CUDA allocations, never a GPU success claim."""
import ctypes
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.tools.jetson import trt_observation as recovery


@pytest.fixture
def runtime(monkeypatch, tmp_path):
  path = Path(os.environ['CARROT_JETSON']) / 'third_party/jetlink/jetlink/server/backends/trt/engine.py'
  events, live, counts = [], {}, {}
  state = {'failure': None, 'execute': True, 'capturing': False, 'address_ok': True, 'shape': (1, 2)}
  def event(name):
    events.append(name)
    counts[name] = counts.get(name, 0) + 1
    if state['failure'] == (name, counts[name]):
      raise RuntimeError('injected ' + name)
  class Cuda:
    def set_device(self, device):
      event('set_device')
    def stream_create(self):
      event('stream_create')
      live['stream'] = 7
      return 7
    def stream_destroy(self, stream):
      event('stream_destroy')
      live.pop('stream')
    def allocate(self, size, kind):
      event(kind)
      buffer = ctypes.create_string_buffer(size)
      address = ctypes.addressof(buffer)
      live[address] = kind, buffer
      return address
    def malloc(self, size):
      return self.allocate(size, 'malloc')
    def host_alloc(self, size):
      return self.allocate(size, 'host_alloc')
    def free(self, address):
      event('free')
      assert live.pop(address)[0] == 'malloc'
    def host_free(self, address):
      event('host_free')
      assert live.pop(address)[0] == 'host_alloc'
    def memcpy_h2d_async(self, dst, src, size, stream):
      event('h2d')
      ctypes.memmove(dst, src, size)
    def memcpy_d2h_async(self, dst, src, size, stream):
      event('d2h')
      ctypes.memmove(dst, src, size)
    def stream_sync(self, stream):
      event('sync')
    def stream_begin_capture(self, stream):
      event('begin')
      state['capturing'] = True
    def stream_end_capture(self, stream):
      state['capturing'] = False
      event('end')
      live['graph'] = 8
      return 8
    def graph_instantiate(self, graph):
      event('instantiate')
      live['exec'] = 9
      return 9
    def graph_destroy(self, graph):
      event('graph_destroy')
      live.pop('graph')
    def graph_exec_destroy(self, graph):
      event('exec_destroy')
      live.pop('exec')
  cuda = Cuda()
  addresses = {}
  class Context:
    def set_tensor_address(self, name, address):
      event('bind')
      addresses[name] = address
      return state['address_ok']
    def execute_async_v3(self, stream):
      event('execute')
      if state['execute']:
        ctypes.memmove(addresses['outputs'], addresses['img'], 4)
      return state['execute']
  class Engine:
    num_io_tensors = 2
    def create_execution_context(self):
      event('context')
      return Context()
    def get_tensor_name(self, index):
      return ('img', 'outputs')[index]
    def get_tensor_shape(self, name):
      return state['shape']
    def get_tensor_dtype(self, name):
      return 'HALF'
    def get_tensor_mode(self, name):
      return 'INPUT' if name == 'img' else 'OUTPUT'
  class Logger:
    WARNING = 2
    def __init__(self, severity):
      pass
  trt = SimpleNamespace(__version__='10.3.0', Logger=Logger, TensorIOMode=SimpleNamespace(INPUT='INPUT'),
    DataType=SimpleNamespace(**{name: name for name in ('FLOAT', 'HALF', 'INT8', 'INT32', 'INT64', 'BOOL', 'UINT8')}),
    Runtime=lambda logger: SimpleNamespace(deserialize_cuda_engine=lambda raw: Engine()))
  monkeypatch.setitem(sys.modules, 'tensorrt', trt)
  # Import the actual engine while keeping the fake CUDA binding process local.
  package = SimpleNamespace(cudart=cuda)
  monkeypatch.setitem(sys.modules, 'jetlink.server.backends.trt', package)
  spec = importlib.util.spec_from_file_location('nexo_observation_vendor', path)
  vendor = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, spec.name, vendor)
  spec.loader.exec_module(vendor)
  package.engine = vendor
  plan = tmp_path / 'test.plan'
  plan.write_bytes(b'simulated engine bytes')
  checked = recovery.engine_class(vendor)
  return SimpleNamespace(vendor=vendor, checked=checked, state=state, events=events, live=live, plan=plan)


@pytest.mark.parametrize('failure', [('stream_create', 1), ('malloc', 1), ('host_alloc', 1),
  ('malloc', 2), ('host_alloc', 2), ('bind', 1), ('bind', 2)])
def test_partial_initialization_releases_every_acquired_resource(runtime, failure):
  r = runtime
  r.state['failure'] = failure
  with pytest.raises(RuntimeError, match='injected ' + failure[0]):
    r.checked(r.plan)
  assert r.live == {}


def test_failed_binding_is_rejected_and_fully_recovered(runtime):
  runtime.state['address_ok'] = False
  with pytest.raises(RuntimeError, match='failed to bind'):
    runtime.checked(runtime.plan)
  assert runtime.live == {}


@pytest.mark.parametrize('stage', ['begin', 'execute', 'end', 'instantiate'])
def test_failed_capture_ends_capture_and_can_run_plain_inference(runtime, stage):
  r = runtime
  engine = r.checked(r.plan)
  r.state['failure'] = (stage, 1)
  assert engine.capture_graph() is False
  assert r.state['capturing'] is False and engine.graph_exec is None
  assert 'graph' not in r.live and 'exec' not in r.live
  r.state['failure'] = None
  engine.host_input('img')[:] = [[1.5, -2.]]
  np.testing.assert_array_equal(engine.run()['outputs'], [[1.5, -2.]])
  engine.close()
  assert r.live == {}


def test_normal_run_inherits_pinned_execution_and_close_is_ordered_and_idempotent(runtime):
  r = runtime
  engine = r.checked(r.plan)
  assert r.checked.run is r.vendor.TrtEngine.run
  assert r.checked._enqueue is r.vendor.TrtEngine._enqueue
  engine.host_input('img')[:] = [[3., 4.]]
  np.testing.assert_array_equal(engine.run()['outputs'], [[3., 4.]])
  assert engine.capture_graph()
  engine.close()
  assert r.events.index('exec_destroy') < r.events.index('free')
  assert r.live == {}
  events = list(r.events)
  engine.close()
  assert r.events == events
  recovery.verify_cleanup(engine)


def test_one_release_failure_does_not_skip_other_resources(runtime, caplog):
  r = runtime
  engine = r.checked(r.plan)
  r.state['failure'] = ('free', 1)
  engine.close()
  assert len(r.live) == 1 and next(iter(r.live.values()))[0] == 'malloc'
  assert r.events.count('host_free') == 2 and 'stream_destroy' in r.events
  assert 'CUDA cleanup failed' in caplog.text
  with pytest.raises(RuntimeError, match='CUDA resource cleanup failed'):
    recovery.verify_cleanup(engine)


def test_unknown_engine_bytes_are_rejected_before_cuda(runtime, tmp_path, monkeypatch):
  path = tmp_path / 'other.py'
  path.write_text('different engine')
  monkeypatch.setattr(runtime.vendor, '__file__', str(path))
  with pytest.raises(ValueError, match='pinned Carrot'):
    recovery.engine_class(runtime.vendor)
  assert not runtime.events


def test_unknown_tensorrt_version_is_rejected_before_cuda(runtime, monkeypatch):
  monkeypatch.setattr(runtime.vendor.trt, '__version__', '10.4.0')
  with pytest.raises(ValueError, match='TensorRT 10.3.0'):
    recovery.engine_class(runtime.vendor)
  assert not runtime.events


def test_backport_is_limited_to_validation_backend_load(runtime):
  original_engine = runtime.vendor.TrtEngine
  class Base:
    device = 0
    def build(self, *args, **kwargs):
      return 'original builder'
    def tag(self):
      return 'original cache identity'
  checked = recovery.backend_class(Base)
  assert checked.build is Base.build and checked.tag is Base.tag
  engine = checked().load(runtime.plan)
  assert engine.safety_profile == recovery.PROFILE
  assert runtime.vendor.TrtEngine is original_engine
  engine.close()
  assert not runtime.live
