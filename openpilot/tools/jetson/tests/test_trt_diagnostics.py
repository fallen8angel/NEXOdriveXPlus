"""Actual NEXO/pinned builder; TensorRT calls simulated, no GPU success claim."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import numpy as np
import onnx
from onnx.reference import ReferenceEvaluator
from onnx.utils import Extractor
import pytest

from openpilot.tools.jetson import onnx_inputs, trt_diagnostics as diag

MODEL = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/models/driving_supercombo.onnx'


def test_actual_model_analysis_preserves_bytes_and_contract():
  before = MODEL.read_bytes()
  result = diag.analyze_model(MODEL)
  assert result['group'] == 64 and result['kernel'] == [64, 1, 3, 3]
  assert result['strides'] == [2, 2] and result['pads'] == [1, 1, 1, 1]
  assert result['input']['shape'] == [1, 64, 64, 128]
  assert result['boundary']['shape'] == result['output']['shape'] == [1, 64, 32, 64]
  assert result['weight_dtype'] == result['input']['dtype'] == result['output']['dtype'] == 'FLOAT16'
  assert result['approximate'] == 'tanh' and not result['arithmetic_rewrite'] and not result['gpu_verified']
  assert result['graph_outputs'] == [{'name': 'outputs', 'dtype': 'FLOAT16', 'shape': [1, 2576]}]
  assert MODEL.read_bytes() == before and hashlib.sha256(before).hexdigest() == diag.MODEL_SHA


def test_unknown_model_is_rejected_without_touching_it(tmp_path):
  path = tmp_path / 'other.onnx'
  path.write_bytes(b'other model')
  with pytest.raises(ValueError, match='exact NEXO model SHA'):
    diag.analyze_model(path)
  assert path.read_bytes() == b'other model'


def test_actual_conv_gelu_math_weights_and_output_are_unchanged():
  original = onnx.load(str(MODEL))
  adapted = onnx_inputs.adapt(original)
  def region(model):
    return Extractor(onnx.shape_inference.infer_shapes(model)).extract_model(['vision/gelu'], ['vision/gelu_1'])
  before, after = region(original), region(adapted)
  assert before.graph.SerializeToString() == after.graph.SerializeToString()
  # Actual depthwise weights and full affected tensor size, including pad/stride.
  rng = np.random.default_rng(20261021)
  feeds = {'vision/gelu': rng.uniform(-2, 2, (1, 64, 64, 128)).astype(np.float16)}
  expected = ReferenceEvaluator(before).run(None, feeds)[0]
  observed = ReferenceEvaluator(after).run(None, feeds)[0]
  assert expected.shape == (1, 64, 32, 64) and np.isfinite(expected).all()
  np.testing.assert_array_equal(observed.view(np.uint16), expected.view(np.uint16))


@pytest.fixture
def builder(monkeypatch, tmp_path):
  root = Path(os.environ.get('CARROT_JETSON', '/missing'))
  path = root / 'third_party/jetlink/jetlink/server/backends/trt/build.py'
  if not path.is_file():
    pytest.skip('CARROT_JETSON must point to the pinned server')
  monkeypatch.syspath_prepend(str(root / 'third_party/jetlink'))
  from jetlink import onnx_patch
  monkeypatch.setattr(onnx_patch, 'patch_file', onnx_patch.patch_file)
  onnx_inputs.install_carrot_patch()
  state = {'build_calls': 0, 'flags': [], 'parse_ok': True, 'plan': b'simulated-plan',
           'console': [], 'workspace': None, 'parse_model': None, 'failure': None}
  class Logger:
    WARNING = 2
    def __init__(self, severity):
      self.severity = severity
    def log(self, severity, message):
      if severity <= self.severity:
        state['console'].append(message)
  class Config:
    def set_flag(self, flag):
      state['flags'].append(flag)
    def set_memory_pool_limit(self, kind, amount):
      state['workspace'] = (kind, amount)
    def create_timing_cache(self, value):
      state['timing_blob'] = value
      return None
  class Builder:
    def __init__(self, logger):
      self.logger = logger
    def create_network(self, flags):
      state['network_flags'] = flags
      return SimpleNamespace(num_layers=450)
    def create_builder_config(self):
      state['config'] = Config()
      return state['config']
    def build_serialized_network(self, network, config):
      state['build_calls'] += 1
      self.logger.log(4, 'Tactic 42 skipped: insufficient workspace (SIMULATED)')
      self.logger.log(1, 'CUDA/tactic failure (SIMULATED)')
      if state['failure']:
        raise state['failure']
      return state['plan']
  class Parser:
    num_errors = 1
    def __init__(self, network, logger):
      pass
    def parse_from_file(self, source):
      state['parse_model'] = onnx.load(source)
      return state['parse_ok']
    def get_error(self, index):
      return 'SIMULATED parser error'
  trt = SimpleNamespace(__version__='10.3.0', ILogger=object, Logger=Logger, Builder=Builder, OnnxParser=Parser,
                        IProgressMonitor=object, BuilderFlag=SimpleNamespace(FP16='FP16'),
                        MemoryPoolType=SimpleNamespace(WORKSPACE='workspace'))
  monkeypatch.setitem(sys.modules, 'tensorrt', trt)
  spec = importlib.util.spec_from_file_location('nexo_test_pinned_builder', path)
  vendor = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(vendor)
  monkeypatch.setattr(vendor, 'available_bytes', lambda: int(5.2 * (1 << 30)))
  monkeypatch.setattr(vendor, 'device_tag', lambda device=0: 'Orin-sm87')
  state.update(vendor=vendor, trt=trt, destination=tmp_path / 'nexo.plan', vendor_bytes=path.read_bytes())
  return state


@pytest.mark.parametrize('failure', ['no_engine', 'cuda_exception', 'parse'])
def test_real_pinned_failure_keeps_previous_engine_and_propagates_once(builder, failure):
  b = builder
  b['destination'].write_bytes(b'previous plan')
  if failure == 'no_engine':
    b['plan'] = None
  elif failure == 'cuda_exception':
    b['failure'] = RuntimeError('SIMULATED CUDA error')
  else:
    b['parse_ok'] = False
  diag.instrument_builder(b['vendor'], b['trt'])
  with pytest.raises(RuntimeError):
    b['vendor'].build_engine(MODEL, b['destination'], timing_cache=b['destination'].with_suffix('.cache'))
  assert b['build_calls'] == (0 if failure == 'parse' else 1)
  assert b['destination'].read_bytes() == b'previous plan'
  info = json.loads(b['destination'].with_suffix('.build.json').read_text())
  assert info['state'] == 'failed' and info['attempts'] == 1 and not info['arithmetic_rewrite']
  if failure != 'parse':
    assert any('Tactic 42' in row['message'] for row in info['log_tail'])
    assert any('CUDA/tactic' in row['message'] for row in info['warning_error_tail'])
    assert not any('Tactic 42' in line for line in b['console'])  # WARNING output unchanged.
  assert Path(b['vendor'].__file__).read_bytes() == b['vendor_bytes']
  assert diag._capture.get() is None


@pytest.mark.parametrize('workspace', [None, 256 << 20])
def test_instrumentation_preserves_build_settings_graph_cache_and_metadata(builder, workspace):
  b = builder
  diag.instrument_builder(b['vendor'], b['trt'])
  original_wrapper = b['vendor'].build_engine
  diag.instrument_builder(b['vendor'], b['trt'])
  assert b['vendor'].build_engine is original_wrapper
  timing = b['destination'].with_suffix('.cache')
  timing.write_bytes(b'old timing cache')
  result = b['vendor'].build_engine(MODEL, b['destination'], workspace=workspace, timing_cache=timing,
                                    meta_extra={'spec': {'sha256': diag.MODEL_SHA}})
  assert result == b['destination'] and b['build_calls'] == 1
  assert b['flags'] == ['FP16'] and b['network_flags'] == 0
  expected = int(int(5.2 * (1 << 30)) * .4) if workspace is None else workspace
  assert b['workspace'] == ('workspace', expected)
  assert b['config'].builder_optimization_level == 3 and b['timing_blob'] == b'old timing cache'
  assert timing.read_bytes() == b'old timing cache'
  assert b['parse_model'].SerializeToString() == onnx_inputs.adapt(onnx.load(str(MODEL))).SerializeToString()
  meta = json.loads(result.with_suffix('.json').read_text())
  assert meta['spec'] == {'sha256': diag.MODEL_SHA} and meta['fp16'] is True
  info = json.loads(result.with_suffix('.build.json').read_text())
  assert info['state'] == 'built' and info['workspace_bytes'] == expected
  assert not info['gpu_validation_claimed'] and not info['arithmetic_rewrite']
  assert b['trt'].Logger is not b['vendor'].trt.Logger  # Actual module never mutated.


@pytest.mark.parametrize('bad', ['version', 'vendor'])
def test_unpinned_runtime_or_builder_is_rejected(builder, tmp_path, bad):
  b = builder
  original = b['vendor'].build_engine
  if bad == 'version':
    b['trt'].__version__ = '10.4.0'
  else:
    path = tmp_path / 'unknown.py'
    path.write_text('# different builder')
    b['vendor'].__file__ = str(path)
  with pytest.raises(ValueError):
    diag.instrument_builder(b['vendor'], b['trt'])
  assert b['vendor'].build_engine is original


def test_bounded_verbose_log_keeps_error_summary(builder):
  b = builder
  def noisy(source, destination, **kwargs):
    logger = b['vendor'].trt.Logger(2)
    logger.log(1, 'early CUDA error')
    for i in range(2100):
      logger.log(4, f'tactic {i}')
    raise RuntimeError('no engine')
  b['vendor'].build_engine = noisy
  diag.instrument_builder(b['vendor'], b['trt'])
  with pytest.raises(RuntimeError, match='no engine'):
    b['vendor'].build_engine(MODEL, b['destination'])
  info = json.loads(b['destination'].with_suffix('.build.json').read_text())
  assert info['message_count'] == 2101 and info['truncated']
  assert len(info['log_tail']) == 2000
  assert info['warning_error_tail'][0]['message'] == 'early CUDA error'


def test_native_thread_callback_and_initial_snapshot(builder):
  b = builder
  def threaded(source, destination, **kwargs):
    initial = json.loads(Path(destination).with_suffix('.build.json').read_text())
    assert initial['state'] == 'building' and initial['workspace_bytes'] > 0
    logger = b['vendor'].trt.Logger(2)
    thread = threading.Thread(target=lambda: logger.log(4, 'native worker tactic details'))
    thread.start()
    thread.join()
    raise RuntimeError('no engine')
  b['vendor'].build_engine = threaded
  diag.instrument_builder(b['vendor'], b['trt'])
  with pytest.raises(RuntimeError, match='no engine'):
    b['vendor'].build_engine(MODEL, b['destination'])
  info = json.loads(b['destination'].with_suffix('.build.json').read_text())
  assert info['log_tail'][0]['message'] == 'native worker tactic details'


def test_diagnostic_write_failure_does_not_mask_build_failure(builder, monkeypatch):
  b = builder
  b['plan'] = None
  original = Path.write_text
  def write(path, *args, **kwargs):
    if path.name.endswith('.build.json.tmp'):
      raise PermissionError('SIMULATED diagnostic disk failure')
    return original(path, *args, **kwargs)
  monkeypatch.setattr(Path, 'write_text', write)
  diag.instrument_builder(b['vendor'], b['trt'])
  with pytest.raises(RuntimeError, match='TensorRT returned no engine'):
    b['vendor'].build_engine(MODEL, b['destination'])
  assert b['build_calls'] == 1 and diag._capture.get() is None


def test_collector_is_read_only_without_runtime_dependencies(tmp_path, monkeypatch):
  runtime = tmp_path / 'runtime'
  engine_dir = runtime / 'cache/engines'
  engine_dir.mkdir(parents=True)
  existing = engine_dir / (diag.MODEL_SHA[:16] + '.trt10.3.0.Orin-sm87.build.json')
  existing.write_text('{"state":"failed"}')
  monkeypatch.setattr(diag, 'RUNTIME', runtime)
  calls = []
  def run(command, **kwargs):
    calls.append(command)
    assert command[0] in ('uname', 'dpkg-query', 'systemctl', 'journalctl', 'ip', 'lsusb', 'ss', 'iw')
    if command[0] == 'iw':
      assert command in (['iw', 'phy'], ['iw', 'dev'])
    if command[0] == 'systemctl':
      assert command[1] == 'show'
    return SimpleNamespace(returncode=0, stdout='read-only state', stderr='')
  monkeypatch.setattr(diag.subprocess, 'run', run)
  monkeypatch.setattr(diag, 'install', lambda: pytest.fail('runtime must not load during collection'))
  report = json.loads(diag.collect(tmp_path / 'collected').read_text())
  assert report['engine_build_attempts'] == 0 and report['read_only_collection']
  assert len(calls) == 12 and report['files'][str(existing)] == '{"state":"failed"}'
  assert existing.read_text() == '{"state":"failed"}'
  with pytest.raises(FileExistsError):
    diag.collect(tmp_path / 'collected')  # Never overwrite an earlier snapshot.


def test_missing_wifi_tool_does_not_prevent_installed_version_collection(tmp_path, monkeypatch):
  runtime = tmp_path / 'runtime'
  (runtime / 'current').mkdir(parents=True)
  (runtime / 'current/SOURCE_COMMIT').write_text('installed-commit')
  monkeypatch.setattr(diag, 'RUNTIME', runtime)
  def run(command, **kwargs):
    if command[0] == 'iw':
      raise FileNotFoundError('iw unavailable')
    return SimpleNamespace(returncode=0, stdout='', stderr='')
  monkeypatch.setattr(diag.subprocess, 'run', run)
  report = json.loads(diag.collect(tmp_path / 'collected').read_text())
  assert report['files'][str(runtime / 'current/SOURCE_COMMIT')] == 'installed-commit'
  assert len([c for c in report['commands'] if c.get('error') == 'iw unavailable']) == 2
  assert report['engine_build_attempts'] == 0
