import json
import sys
from types import SimpleNamespace

import numpy as np
import onnx
from onnx import TensorProto as T, helper as h
import pytest

from openpilot.tools.jetson import nexo_n16_gpu_trace as trace, nexo_n16_split_gpu as split
from openpilot.tools.jetson.nexo_n16_cpu_precision import cpu_session
from openpilot.tools.jetson.nexo_n16_diagnostics import classify_trial
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import save_arrays
from openpilot.tools.jetson.nexo_n16_trt_trial import trial


def left_model():
  def value(name, dtype):
    return h.make_tensor_value_info(name, dtype, [1, 4])
  graph = h.make_graph([
    h.make_node('Identity', ['state_img_q'], ['next_state_img_q']),
    h.make_node('Cast', ['new_img'], ['pixels'], to=T.FLOAT),
    h.make_node('Add', ['pixels', 'bias'], ['boundary'], name='sum'),
  ], 'trace_left', [value('new_img', T.UINT8), value('state_img_q', T.UINT8)],
     [value('boundary', T.FLOAT), value('next_state_img_q', T.UINT8)],
     initializer=[h.make_tensor('bias', T.FLOAT, [1], [0.25])],
     value_info=[value('pixels', T.FLOAT)])
  return h.make_model(graph, ir_version=10, opset_imports=[h.make_opsetid('', 17)])


@pytest.mark.parametrize('damage,expected', [
  ('none', 'within_tolerance_at_checkpoint'),
  ('output', 'output_exceeds_tolerance_with_operands_in_tolerance'),
  ('operand', 'trace_upstream_operands'),
  ('tap', 'instrumentation_changed_boundary'),
])
def test_trace_keeps_plain_engine_as_control_and_identifies_mismatching_operands(tmp_path, monkeypatch, damage, expected):
  source = tmp_path / 'left.onnx'
  onnx.save(left_model(), source)
  before = source.read_bytes()
  save_arrays(tmp_path / 'first-boundary.npz', {'boundary': np.full((1, 4), 0.25, np.float32)})
  trace.trace_cpu(tmp_path, ['boundary'], patched=False)
  meta = trace.trace_cpu(tmp_path, ['boundary'], patched=True)
  assert meta['boundary_errors'] == {'boundary': 0}
  (tmp_path / 'trace_patched-result.json').write_text(json.dumps(meta))
  split.prepare_half(tmp_path, 'left')
  for stem in ('left', 'left-trace'):
    plan = tmp_path / (stem + '.plan')
    plan.write_bytes(stem.encode())
    (tmp_path / ('build_' + stem.replace('-', '_') + '-result.json')).write_text(
      json.dumps({'bytes': plan.stat().st_size, 'sha256': split.digest(plan)}))
  closed = []
  monkeypatch.setitem(sys.modules, 'jetlink.server.backends.trt.engine',
                      SimpleNamespace(TrtEngine=lambda p: SimpleNamespace(close=lambda: closed.append(p))))
  class EmulatedSession:
    def __init__(self, engine, path):
      self.session = cpu_session(path)
      self.traced = path.name == 'left-trace-trt.onnx'
    def get_inputs(self):
      return self.session.get_inputs()
    def run(self, names, feeds):
      outputs = self.session.run(names, feeds)
      if damage in ('output', 'operand') or (damage == 'tap' and self.traced):
        outputs[names.index('boundary')] += 1
      if damage == 'operand' and self.traced:
        outputs[names.index('pixels')] += 1
      return outputs
  monkeypatch.setattr(split, 'GpuSession', EmulatedSession)
  trace.replay_trace(tmp_path, baseline=True)
  report = trace.replay_trace(tmp_path, baseline=False)
  assert len(closed) == 2 and source.read_bytes() == before
  point = report['earliest_sampled_mismatch']
  assert (point and point['tensor']) == {'none': None, 'tap': None, 'operand': 'pixels', 'output': 'boundary'}[damage]
  assert report['producers']['boundary']['finding'] == expected
  assert report['producers']['boundary']['divergent_operands'] == (['pixels'] if damage == 'operand' else [])
  assert not report['gpu_parity_verified']
  diagnosis = classify_trial({**report, 'trace_targets': ['boundary']})
  assert diagnosis['diagnosis_code'] == 'offline_gpu_left_trace_only' and not diagnosis['synthetic_gpu_verified']


def test_trace_checks_metadata_bounds_and_whole_cpu_anchor(tmp_path):
  model = left_model()
  with pytest.raises(ValueError, match='Missing left-side'):
    trace.instrument(model, ['invented'])
  model.graph.value_info[0].type.tensor_type.shape.dim[1].dim_value = 1 << 24
  with pytest.raises(ValueError, match='memory bound'):
    trace.instrument(model, ['boundary'])
  onnx.save(left_model(), tmp_path / 'left.onnx')
  save_arrays(tmp_path / 'first-boundary.npz', {'boundary': np.zeros((1, 4), dtype=np.float32)})
  with pytest.raises(ValueError, match='verified first-frame boundary'):
    trace.trace_cpu(tmp_path, ['boundary'], patched=False)


@pytest.mark.parametrize('gpu,targets', [(False, []), (True, ['a', 'a']), (True, ['a', 'b', 'c'])])
def test_invalid_trace_options_stop_before_device_access(gpu, targets):
  with pytest.raises(ValueError, match='trace requires'):
    trial(lambda: pytest.fail('device access'), partition_gpu_parity=gpu, partition_cut=360, trace_targets=targets)


def test_trace_runs_one_reference_and_serial_left_builds_with_existing_cpu_receipt(tmp_path, monkeypatch):
  from openpilot.tools.jetson.tests.test_nexo_n16_split_gpu import accepted, SPEC
  receipt = accepted()
  monkeypatch.setattr(split, 'load_registry', lambda: (SPEC, None))
  calls = []
  def isolated(target, args, role, *a, **kw):
    calls.append(role)
    result = {'controller_and_cpu_worker_rss_bytes_sampled': 1024, 'ort_version': 'test'}
    if role == 'reference':
      assert args[-1] == trace.DEFAULT_TARGETS
      result['frames'] = 1
    elif role == 'probe':
      result['plan'] = receipt['boundary_transport']
    elif role == 'build_left':
      (tmp_path / 'left.plan').write_bytes(b'trial')
    elif role == 'gpu_plain':
      result['boundary_errors'] = {}
    elif role == 'build_left_trace':
      assert not (tmp_path / 'left.plan').exists()
      assert kw['timeout_s'] == 900
    elif role == 'gpu_trace':
      result['state'] = 'gpu_left_trace_complete'
    return result
  monkeypatch.setattr(split, 'isolated_phase', isolated)
  result = split.run_offline(tmp_path, tmp_path, tmp_path, receipt, lambda _: None, trace.DEFAULT_TARGETS)
  assert calls == ['reference', 'probe', 'trace_original', 'trace_patched', 'build_left', 'gpu_plain', 'build_left_trace', 'gpu_trace']
  assert result['state'] == 'gpu_left_trace_complete'


def test_encoder_stage_preset_resolves_names_of_actual_producer_ports():
  # ORT can rename internal FP32 outputs. Identify them by the original node and port.
  graph = h.make_graph([
    h.make_node('Conv', ['image', 'weight'], ['stem'], name='node_conv2d'),
    h.make_node('LayerNormalization', ['stem', 'scale'], ['norm'], name='node_layer_norm'),
    h.make_node('Add', ['norm', 'stem'], ['residual6'], name='node_add_6'),
    h.make_node('Add', ['residual6', 'norm'], ['residual16'], name='node_add_16'),
  ], 'stage_ports', [h.make_tensor_value_info('image', T.FLOAT, [1, 1, 2, 2])],
     [h.make_tensor_value_info('residual16', T.FLOAT, [1, 1, 2, 2])],
     initializer=[h.make_tensor('weight', T.FLOAT, [1, 1, 1, 1], [1.]), h.make_tensor('scale', T.FLOAT, [2], [1., 1.])],
     value_info=[h.make_tensor_value_info(n, T.FLOAT, [1, 1, 2, 2]) for n in ('stem', 'norm', 'residual6')])
  model = h.make_model(graph)
  meta, names = trace.instrument(model, ['encoder-stages'])
  assert list(meta) == names == ['stem', 'norm', 'residual6', 'residual16']
  assert len(model.graph.output) == 4
  model.graph.node[0].name = 'other'
  with pytest.raises(ValueError, match='identify N16 encoder checkpoint'):
    trace.instrument(model, ['encoder-stages'])
