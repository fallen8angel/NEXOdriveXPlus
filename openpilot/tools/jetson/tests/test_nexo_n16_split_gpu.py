"""Cut-360 offline GPU orchestration and strict replay tests; no device claims."""
import json
from types import SimpleNamespace
import sys

import numpy as np
import onnx
import pytest

from openpilot.tools.jetson import nexo_n16_split_gpu as gpu
from openpilot.tools.jetson.nexo_n16_cpu_precision import cpu_session, widen_boundary
from openpilot.tools.jetson.nexo_n16_cpu_workers import write_reference
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import ort_dtype, save_arrays, synthetic_feeds
from openpilot.tools.jetson.nexo_n16_trt_trial import trial
from openpilot.tools.jetson.tests.test_nexo_n16_cpu_precision import graphs, SPEC


def accepted():
  plan = {'mode': 'resolved_whole_cpu_graph', 'probe_public_outputs_exact': True,
          'boundary': [{'tensor': str(n)} for n in range(9)]}
  return {'state': 'cpu_parity_passed', 'model_sha256': gpu.MODEL_SHA, 'policy': gpu.POLICY,
          'cut_node_index': 360, 'frames_completed': 140, 'distinct_image_frames': 140,
          'cpu_numerical_parity_verified': True, 'stateful_140_frames_verified': True, 'failed_output_count': 0,
          'initializer_integrity_verified': True, 'boundary_transport': plan,
          'first_frame_boundary_max_abs_error': {str(n): 0.0 for n in range(9)},
          'max_abs_error_by_output': dict.fromkeys(list(SPEC['output_shapes']) + ['outputs:' + k for k in SPEC['output_slices']], 0.0), 'ort_version': 'test'}


@pytest.mark.parametrize('change', [{'cut_node_index': 361}, {'model_sha256': 'x'}, {'frames_completed': 139},
                                    {'initializer_integrity_verified': False}, {'state': 'running'},
                                    {'first_frame_boundary_max_abs_error': {}}, {'policy': {'atol': 0.01}},
                                    {'failed_output_count': 1}, {'max_abs_error_by_output': {}}])
def test_ineligible_cpu_receipt_stops_before_any_worker(tmp_path, monkeypatch, change):
  monkeypatch.setattr(gpu, 'load_registry', lambda: (SPEC, None))
  monkeypatch.setattr(gpu, 'isolated_phase', lambda *a, **kw: pytest.fail('worker launched without CPU parity'))
  with pytest.raises(ValueError, match='passing local'):
    gpu.run_offline(tmp_path, tmp_path, tmp_path, {**accepted(), **change}, lambda _: None)


def test_verified_plan_required_before_serial_build_and_replay(tmp_path, monkeypatch):
  monkeypatch.setattr(gpu, 'load_registry', lambda: (SPEC, None))
  receipt = accepted()
  calls = []
  def isolated(target, args, role, *a, **kw):
    calls.append(role)
    assert kw['timeout_s'] == (900 if role.startswith('build_') else None)
    return {'ort_version': 'test', 'frames': 140, 'plan': receipt['boundary_transport'].copy(),
            'controller_and_cpu_worker_rss_bytes_sampled': 1024}
  monkeypatch.setattr(gpu, 'isolated_phase', isolated)
  result = gpu.run_offline(tmp_path, tmp_path, tmp_path, receipt, lambda _: None)
  assert calls == ['reference', 'probe', 'build_left', 'build_right', 'gpu_replay']
  assert not result['production_activation_allowed']
  receipt['ort_version'] = 'other'
  calls.clear()
  with pytest.raises(ValueError, match='CPU runtime'):
    gpu.run_offline(tmp_path, tmp_path, tmp_path, receipt, lambda _: None)
  assert calls == ['reference']


def test_gpu_mode_requires_exact_cut_and_excludes_other_trials():
  for kwargs in ({'partition_cut': 361}, {'partition_cut': 360, 'partition_stateful_cpu_only': True},
                 {'partition_cut': 360, 'workspace_mib': 256}):
    with pytest.raises(ValueError):
      trial(lambda: pytest.fail('device preflight reached'), partition_gpu_parity=True, **kwargs)


@pytest.mark.parametrize('damage', [False, True])
def test_gpu_replay_with_real_cpu_oracle_and_reused_device_output_buffers(tmp_path, monkeypatch, damage):
  original, left, right, tensors = graphs(stateful=True)
  reference = cpu_session(original)
  spec = {**SPEC, 'input_shapes': {i.name: i.shape for i in reference.get_inputs()}, 'sha256': gpu.MODEL_SHA}
  write_reference(reference, spec, tmp_path / 'reference.xz', lambda _: None)
  sessions = {}
  for side, model in (('left', left), ('right', right)):
    widen_boundary(model, tensors, side)
    onnx.save_model(model, tmp_path / ('left-trt.onnx' if side == 'left' else 'right.onnx'))
    sessions[side] = cpu_session(model)
    plan = tmp_path / (side + '.plan')
    plan.write_bytes(side.encode())
    (tmp_path / ('build_' + side + '-result.json')).write_text(json.dumps({'bytes': len(side), 'sha256': gpu.digest(plan)}))
  lhs = sessions['left']
  save_arrays(tmp_path / 'first-boundary.npz', {'bridge': lhs.run(None, synthetic_feeds(lhs.get_inputs(), 0))[0]})
  (tmp_path / 'probe-result.json').write_text(json.dumps({'plan': {'boundary': [{'tensor': 'bridge'}]}}))
  closed = []
  class Engine:
    def __init__(self, path):
      self.name = 'left' if path.endswith('left.plan') else 'right'
      self.session = sessions[self.name]
      def bindings(items):
        return {i.name: SimpleNamespace(dtype=ort_dtype(i.type), shape=tuple(i.shape), host=np.empty(i.shape, ort_dtype(i.type)))
                for i in items}
      self.inputs = bindings(self.session.get_inputs())
      self.outputs = bindings(self.session.get_outputs())
    def host_input(self, name):
      return self.inputs[name].host
    def run(self):
      out = self.session.run(None, {k: v.host for k, v in self.inputs.items()})
      for binding, array in zip(self.outputs.values(), out, strict=True):
        np.copyto(binding.host, array)
      if damage and self.name == 'right':
        self.outputs['next_state_feat_q'].host += np.float16(0.25)
      return {k: v.host for k, v in self.outputs.items()}
    def close(self):
      closed.append(self.name)
  monkeypatch.setitem(sys.modules, 'jetlink.server.backends.trt.engine', SimpleNamespace(TrtEngine=Engine))
  phases = []
  result = gpu.replay_gpu(tmp_path, spec, phases.append)
  assert result['frames_completed'] == len(result['left_ms']) == len(result['right_ms']) == 140
  assert result['gpu_parity_verified'] is not damage
  assert result['max_ms'] >= result['p95_ms'] >= result['p50_ms'] > 0
  assert result['paired_engine_fps'] == pytest.approx(1000 / result['mean_ms'])
  assert result['split_frame_ms']['p95'] > 0
  assert phases[-1] == 'gpu_frame_139' and closed == ['right', 'left']
  if damage:
    assert result['failed_output_count'] and result['max_abs_error_by_output']['next_state_feat_q'] > 0.005
  (tmp_path / 'right.plan').write_bytes(b'wrong')
  closed.clear()
  with pytest.raises(ValueError, match='engine identity'):
    gpu.replay_gpu(tmp_path, spec, phases.append)
  assert closed == ['left']


def test_missing_cuda_measurements_are_unknown():
  summary = gpu.memory_summary({'build_left': {}, 'gpu_replay': {'samples': {
    'load': {'cuda_memory_available': True, 'cuda_free_bytes': 800, 'cuda_total_bytes': 1000},
    'frame': {'cuda_memory_available': True, 'cuda_free_bytes': 600, 'cuda_total_bytes': 1000}}}})
  assert summary['build_left']['max_device_used_bytes_sampled'] is None
  assert summary['gpu_replay'] == {'cuda_samples': 2, 'min_device_free_bytes': 600, 'max_device_used_bytes_sampled': 400}


@pytest.mark.parametrize('parse_ok', [True, False])
def test_builder_uses_relative_external_weights_and_verified_resource_limits(tmp_path, monkeypatch, parse_ok):
  from collections import deque
  from pathlib import Path
  mib = 1 << 20
  class Config:
    max_aux_streams = 3
    def __init__(self):
      self.limits = {}
    def set_memory_pool_limit(self, pool, count):
      self.limits[pool] = count
    def get_memory_pool_limit(self, pool):
      return self.limits[pool]
    def clear_flag(self, flag):
      assert flag == 'tf32'
  config = Config()
  class Builder:
    max_threads = 8
    def __init__(self, logger): pass
    def create_network(self, flags):
      assert self.max_threads == 1 and flags == 4
      return object()
    def create_builder_config(self):
      return config
    def build_serialized_network(self, network, conf):
      assert conf.max_aux_streams == conf.builder_optimization_level == 0
      assert conf.limits == {'workspace': 256 * mib, 'dram': 512 * mib}
      return b'test-plan'
  class Parser:
    num_errors = 1
    def __init__(self, network, logger): pass
    def parse_from_file(self, filename):
      assert Path(filename) == tmp_path / 'right.onnx'
      return parse_ok
    def get_error(self, index):
      return 'unsupported operator'
  trt = SimpleNamespace(ILogger=object, Builder=Builder, OnnxParser=Parser,
                        BuilderFlag=SimpleNamespace(TF32='tf32'), NetworkDefinitionCreationFlag=SimpleNamespace(STRONGLY_TYPED=2),
                        MemoryPoolType=SimpleNamespace(WORKSPACE='workspace', TACTIC_DRAM='dram'))
  monkeypatch.setattr(gpu.shutil, 'disk_usage', lambda _: SimpleNamespace(free=4 << 30))
  if parse_ok:
    result = gpu.build_half(tmp_path, 'right', trt, lambda _: None, deque())
    assert result['sha256'] == gpu.digest(tmp_path / 'right.plan')
  else:
    with pytest.raises(RuntimeError, match='unsupported operator'):
      gpu.build_half(tmp_path, 'right', trt, lambda _: None, deque())
    assert not (tmp_path / 'right.plan').exists()


def test_cpu_receipt_preserved_and_gpu_failure_visible_to_diagnostics(tmp_path, monkeypatch):
  from openpilot.tools.jetson import nexo_n16_trt_trial as parent
  from openpilot.tools.jetson.manage import n16_partition_parity_status
  root = tmp_path
  cache = root / 'cache'
  cache.mkdir()
  config = root / 'updates/nexo-ncm.json'
  config.parent.mkdir()
  config.write_bytes(b'display only')
  release = root / 'release'
  release.mkdir()
  (root / 'current').symlink_to(release)
  original = cache / 'nexo-n16-stateful-cpu-parity-result.json'
  original.write_text(json.dumps(accepted()))
  before = original.read_bytes()
  class Process:
    pid = 12345
    exitcode = 0
    def __init__(self, target, args):
      self.stage = args[1]
      assert args[-1] is True
    def start(self):
      (self.stage / 'result.json').write_text(json.dumps({'state': 'failed', 'phase': 'parse_right', 'error': 'unsupported node'}))
      (self.stage / 'build_right-telemetry.json').write_text(json.dumps({'phase': 'parse_right', 'samples': {}, 'warnings': ['node']}))
      (self.stage / 'left.plan').write_bytes(b'trial only')
    def is_alive(self): return False
  monkeypatch.setattr(parent, 'ROOT', root)
  monkeypatch.setattr(parent, 'readiness', lambda *args, **kwargs: (release, config.read_bytes(), 8 << 30, 6 << 30))
  monkeypatch.setattr(parent.multiprocessing, 'get_context', lambda _: SimpleNamespace(Process=Process))
  result = parent.trial(lambda: True, partition_gpu_parity=True, partition_cut=360)
  assert result['state'] == 'failed' and result['diagnosis']['diagnosis_code'] == 'offline_gpu_stage_failed'
  assert not result['gpu_inference_verified'] and not result['production_engine_published']
  assert result['gpu_telemetry']['build_right']['warnings'] == ['node']
  assert not list(cache.glob('nexo-n16-trial-*')) and original.read_bytes() == before
  assert n16_partition_parity_status(root)['offline_split_gpu_validation']['phase'] == 'parse_right'
