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


@pytest.mark.parametrize('carrot_profile', [True, False])
def test_trace_runs_one_reference_and_serial_left_builds_with_existing_cpu_receipt(tmp_path, monkeypatch, carrot_profile):
  from openpilot.tools.jetson.tests.test_nexo_n16_split_gpu import accepted, SPEC
  receipt = accepted()
  monkeypatch.setattr(split, 'load_registry', lambda: (SPEC, None))
  calls = []
  def isolated(target, args, role, *a, **kw):
    calls.append(role)
    result = {'controller_and_cpu_worker_rss_bytes_sampled': 1024, 'ort_version': 'test'}
    if role.startswith('build_') or role in ('trace_original', 'trace_patched'):
      assert (args[-1] is True) == carrot_profile
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
  result = split.run_offline(tmp_path, tmp_path, tmp_path, receipt, lambda _: None, trace.DEFAULT_TARGETS, carrot_profile=carrot_profile)
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


def test_conv_precision_evidence_records_dtype_weight_hash_and_does_not_mutate_model():
  graph = h.make_graph([
    h.make_node('Conv', ['image', 'weight', 'bias'], ['stem'], name='node_conv2d',
                pads=[1, 1, 1, 1], strides=[2, 2]),
  ], 'conv_evidence', [h.make_tensor_value_info('image', T.FLOAT16, [1, 3, 8, 8])],
     [h.make_tensor_value_info('stem', T.FLOAT16, [1, 4, 4, 4])],
     initializer=[
       h.make_tensor('weight', T.FLOAT16, [4, 3, 3, 3], [0.125] * 108),
       h.make_tensor('bias', T.FLOAT16, [4], [0.0] * 4),
     ])
  model = h.make_model(graph)
  before = model.SerializeToString()
  evidence = trace.conv_precision_evidence(model)
  assert evidence['input']['dtype'] == 'FLOAT16'
  assert evidence['weight']['dtype'] == 'FLOAT16'
  assert evidence['weight']['shape'] == [4, 3, 3, 3]
  assert len(evidence['weight']['sha256']) == 64
  assert evidence['bias']['present'] is True
  assert evidence['attributes'] == {'pads': [1, 1, 1, 1], 'strides': [2, 2]}
  assert evidence['precision_root_cause_confirmed'] is False
  assert model.SerializeToString() == before


def test_conv_precision_evidence_fails_closed_on_missing_or_ambiguous_conv():
  model = left_model()
  with pytest.raises(ValueError, match='uniquely identify'):
    trace.conv_precision_evidence(model)
  graph = h.make_graph([
    h.make_node('Conv', ['x', 'w'], ['a'], name='node_conv2d'),
    h.make_node('Conv', ['x', 'w'], ['b'], name='node_conv2d'),
  ], 'ambiguous', [h.make_tensor_value_info('x', T.FLOAT, [1, 1, 2, 2])],
     [h.make_tensor_value_info('a', T.FLOAT, [1, 1, 2, 2]),
      h.make_tensor_value_info('b', T.FLOAT, [1, 1, 2, 2])],
     initializer=[h.make_tensor('w', T.FLOAT, [1, 1, 1, 1], [1.])])
  with pytest.raises(ValueError, match='uniquely identify'):
    trace.conv_precision_evidence(h.make_model(graph))


def test_add_fp32_conv_reference_is_sidecar_only_and_preserves_original_model():
  graph = h.make_graph([
    h.make_node('Conv', ['image', 'weight', 'bias'], ['stem'], name='node_conv2d',
                pads=[1, 1, 1, 1], strides=[2, 2]),
  ], 'conv_fp32_reference', [h.make_tensor_value_info('image', T.FLOAT16, [1, 3, 8, 8])],
     [h.make_tensor_value_info('stem', T.FLOAT16, [1, 4, 4, 4])],
     initializer=[
       h.make_tensor('weight', T.FLOAT16, [4, 3, 3, 3], [0.125] * 108),
       h.make_tensor('bias', T.FLOAT16, [4], [0.0] * 4),
     ])
  model = h.make_model(graph)
  original_node = model.graph.node[0].SerializeToString()
  original_initializers = [value.SerializeToString() for value in model.graph.initializer]
  result = trace.add_fp32_conv_reference(model)
  assert result['output_name'] == 'nexo_n16_conv2d_fp32_reference'
  assert result['original_output_name'] == 'stem'
  assert result['weights_modified'] is False
  assert result['original_conv_modified'] is False
  assert model.graph.node[0].SerializeToString() == original_node
  assert [value.SerializeToString() for value in model.graph.initializer[:2]] == original_initializers
  assert len(model.graph.initializer) == 4
  constants = {value.name: value for value in model.graph.initializer}
  for suffix, name in (('_weight', 'weight'), ('_bias', 'bias')):
    copy = constants[result['output_name'] + suffix]
    assert copy.data_type == T.FLOAT
    np.testing.assert_array_equal(onnx.numpy_helper.to_array(copy),
                                  onnx.numpy_helper.to_array(constants[name]).astype(np.float32))
  output = next(value for value in model.graph.output if value.name == result['output_name'])
  assert output.type.tensor_type.elem_type == T.FLOAT
  added = [node for node in model.graph.node if node.name.startswith('nexo_n16_conv2d_fp32_reference')]
  assert [node.op_type for node in added] == ['Cast', 'Conv']
  assert all(name in constants for name in added[-1].input[1:])
  assert list(next(a for a in added[-1].attribute if a.name == 'kernel_shape').ints) == [3, 3]
  assert result['reference_constants'] == 'static_exact_fp32_copies'
  onnx.checker.check_model(model)


def test_add_fp32_conv_reference_fails_closed_on_non_fp16_conv():
  graph = h.make_graph([
    h.make_node('Conv', ['image', 'weight'], ['stem'], name='node_conv2d'),
  ], 'conv_wrong_type', [h.make_tensor_value_info('image', T.FLOAT, [1, 1, 4, 4])],
     [h.make_tensor_value_info('stem', T.FLOAT, [1, 1, 4, 4])],
     initializer=[h.make_tensor('weight', T.FLOAT, [1, 1, 1, 1], [1.])])
  with pytest.raises(ValueError, match='not the expected FP16'):
    trace.add_fp32_conv_reference(h.make_model(graph))


def test_fp32_reference_rejects_missing_bias_and_channel_mismatch():
  graph = h.make_graph([
    h.make_node('Conv', ['image', 'weight', 'missing'], ['stem'], name='node_conv2d'),
  ], 'missing_bias', [h.make_tensor_value_info('image', T.FLOAT16, [1, 3, 8, 8])],
     [h.make_tensor_value_info('stem', T.FLOAT16, [1, 4, 6, 6])],
     initializer=[h.make_tensor('weight', T.FLOAT16, [4, 2, 3, 3], [0.125] * 72)])
  model = h.make_model(graph)
  with pytest.raises(ValueError, match='metadata is incomplete'):
    trace.add_fp32_conv_reference(model)
  del model.graph.node[0].input[2]
  with pytest.raises(ValueError, match='channel/group mismatch'):
    trace.add_fp32_conv_reference(model)
  assert len(model.graph.initializer) == 1 and len(model.graph.node) == 1


def external_conv_model(stage):
  """Match the compact ORT graph: external FP16 weights beside left.onnx."""
  def value(name, dtype, shape):
    return h.make_tensor_value_info(name, dtype, shape)
  shape = [1, 2, 4, 4]
  weight = (np.arange(24, dtype=np.float16).reshape(3, 2, 2, 2) - 12) / 16
  bias = np.array([-0.25, 0.125, 0.5], dtype=np.float16)
  graph = h.make_graph([
    h.make_node('Identity', ['state_img_q'], ['next_state_img_q']),
    h.make_node('Cast', ['new_img'], ['div'], to=T.FLOAT16),
    h.make_node('Conv', ['div', 'weight', 'bias'], ['stem'], name='node_conv2d', strides=[2, 2]),
  ], 'external_conv',
     [value('new_img', T.UINT8, shape), value('state_img_q', T.UINT8, shape)],
     [value('stem', T.FLOAT16, [1, 3, 2, 2]), value('next_state_img_q', T.UINT8, shape)],
     initializer=[onnx.numpy_helper.from_array(weight, name='weight'),
                  onnx.numpy_helper.from_array(bias, name='bias')],
     value_info=[value('div', T.FLOAT16, shape)])
  model = h.make_model(graph, ir_version=10, opset_imports=[h.make_opsetid('', 17)])
  onnx.save_model(model, stage / 'left.onnx', save_as_external_data=True,
                  all_tensors_to_one_file=True, location='weights.bin', size_threshold=0)
  return weight, bias


def test_external_conv_reference_reads_only_exact_weights_without_mutation(tmp_path, monkeypatch):
  stage = tmp_path / 'stage'
  stage.mkdir()
  weight, bias = external_conv_model(stage)
  monkeypatch.chdir(tmp_path)  # deliberately not the ONNX directory
  path = stage / 'left.onnx'
  source = path.read_bytes()
  backing = (stage / 'weights.bin').read_bytes()
  model = onnx.load(path, load_external_data=False)
  original = [v.SerializeToString() for v in model.graph.initializer]
  assert all(v.data_location == T.EXTERNAL for v in model.graph.initializer)
  result = trace.add_fp32_conv_reference(model, external_dir=stage)
  constants = {v.name: v for v in model.graph.initializer}
  assert result['source_external_data'] is True
  for suffix, expected in (('_weight', weight), ('_bias', bias)):
    copy = constants[result['output_name'] + suffix]
    assert copy.data_location == T.DEFAULT and not copy.external_data
    np.testing.assert_array_equal(onnx.numpy_helper.to_array(copy), expected.astype(np.float32))
  assert [v.SerializeToString() for v in model.graph.initializer[:2]] == original
  assert path.read_bytes() == source and (stage / 'weights.bin').read_bytes() == backing
  # Saving the instrumented graph must not absorb or rewrite external weights.
  saved = stage / 'sidecar.onnx'
  onnx.save_model(model, saved)
  assert (stage / 'weights.bin').read_bytes() == backing
  onnx.checker.check_model(str(saved))


@pytest.mark.parametrize('damage', ['location', 'offset', 'length', 'missing', 'symlink', 'truncated'])
def test_external_conv_reference_fails_closed_on_invalid_backing_data(tmp_path, damage):
  external_conv_model(tmp_path)
  model = onnx.load(tmp_path / 'left.onnx', load_external_data=False)
  tensor = model.graph.initializer[0]
  fields = {v.key: v for v in tensor.external_data}
  backing = tmp_path / 'weights.bin'
  if damage in ('location', 'offset', 'length'):
    fields[damage].value = {'location': '../weights.bin', 'offset': '999999999', 'length': '1'}[damage]
  elif damage == 'missing':
    backing.unlink()
  elif damage == 'symlink':
    backing.rename(tmp_path / 'other.bin')
    backing.symlink_to(tmp_path / 'other.bin')
  else:
    backing.write_bytes(b'')
  before = model.SerializeToString()
  with pytest.raises((ValueError, OSError)):
    trace.add_fp32_conv_reference(model, external_dir=tmp_path)
  assert model.SerializeToString() == before


def test_trace_cpu_external_conv_reference_end_to_end(tmp_path, monkeypatch):
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import synthetic_feeds, load_arrays
  stage = tmp_path / 'stage'
  stage.mkdir()
  external_conv_model(stage)
  monkeypatch.chdir(tmp_path)
  source = stage / 'left.onnx'
  before = source.read_bytes()
  weights_before = (stage / 'weights.bin').read_bytes()
  session = cpu_session(source)
  expected = session.run(['stem'], synthetic_feeds(session.get_inputs(), 0))[0]
  del session
  save_arrays(stage / 'first-boundary.npz', {'stem': expected})
  trace.trace_cpu(stage, ['stem'], patched=False)
  meta = trace.trace_cpu(stage, ['stem'], patched=True)
  fp32 = meta['fp32_conv_reference']
  assert fp32['source_external_data'] is True
  values = load_arrays(stage / 'trace-patched.npz')
  assert values[fp32['output_name']].dtype == np.float32
  assert meta['boundary_errors'] == {'stem': 0}
  assert source.read_bytes() == before and (stage / 'weights.bin').read_bytes() == weights_before


def test_independent_conv_matches_explicit_double_precision_sum():
  from openpilot.tools.jetson.nexo_n16_conv_isolated import independent_conv
  x = np.arange(32, dtype=np.float16).reshape(1, 2, 4, 4) / 8
  w = (np.arange(24, dtype=np.float16).reshape(3, 2, 2, 2) - 12) / 16
  b = np.array([-.25, .125, .5], dtype=np.float16)
  expected = np.empty((1, 3, 2, 2), dtype=np.float64)
  for o in range(3):
    for y in range(2):
      for z in range(2):
        expected[0, o, y, z] = sum(
          float(x[0, i, y*2+dy, z*2+dx]) * float(w[o, i, dy, dx])
          for i in range(2) for dy in range(2) for dx in range(2)) + float(b[o])
  np.testing.assert_array_equal(independent_conv(x, w, b), expected)
  with pytest.raises(ValueError, match='nonoverlapping'):
    independent_conv(x[:, :, :3, :], w, b)


def test_isolated_conv_real_external_data_and_separate_engines(tmp_path, monkeypatch):
  from openpilot.tools.jetson import nexo_n16_conv_isolated as isolated
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import synthetic_feeds, load_arrays
  stage = tmp_path / 'stage'
  stage.mkdir()
  external_conv_model(stage)
  monkeypatch.chdir(tmp_path)
  source = stage / 'left.onnx'
  before = source.read_bytes(), (stage / 'weights.bin').read_bytes()
  session = cpu_session(source)
  expected = session.run(['stem'], synthetic_feeds(session.get_inputs(), 0))[0]
  del session
  save_arrays(stage / 'first-boundary.npz', {'stem': expected})
  meta = trace.trace_cpu(stage, isolated.PRESET, patched=False)
  assert meta['names'] == ['div', 'stem']
  assert meta['fp32_conv_reference'] is None
  result = isolated.prepare_isolated_conv(stage)
  assert len(result['cases']) == 2
  assert result['production_activation_allowed'] is False
  arrays = load_arrays(stage / 'conv-isolated-inputs.npz')
  for index, row in enumerate(result['cases']):
    oracle = arrays[f'oracle{index}']
    assert row['fp16_rounding_only_vs_fp64_oracle']['max_abs_error'] == np.max(np.abs(oracle - oracle.astype(np.float16)))
  assert arrays['oracle0'].dtype == np.float64
  np.testing.assert_array_equal(arrays['original_graph_cpu0'], expected)
  assert len(result['fp16_weight_payload_sha256']) == len(result['fp16_bias_payload_sha256']) == 64
  assert not np.array_equal(arrays['input0'], arrays['input1'])
  for side, dtype in (('conv16', T.FLOAT16), ('conv32', T.FLOAT)):
    model = onnx.load(stage / (side + '.onnx'))
    assert len(model.graph.node) == 1 and model.graph.node[0].op_type == 'Conv'
    assert model.graph.input[0].type.tensor_type.elem_type == dtype
    assert all(v.data_type == dtype and not v.external_data for v in model.graph.initializer)
    onnx.checker.check_model(model)
    plan = stage / (side + '.plan')
    plan.write_bytes(side.encode())
    (stage / ('build_' + side + '-result.json')).write_text(
      json.dumps({'bytes': plan.stat().st_size, 'sha256': split.digest(plan)}))
  closed = []
  monkeypatch.setitem(sys.modules, 'jetlink.server.backends.trt.engine',
                      SimpleNamespace(TrtEngine=lambda p: SimpleNamespace(close=lambda: closed.append(p))))
  class EmulatedSession:
    def __init__(self, engine, path):
      self.session = cpu_session(path)
    def run(self, names, feeds):
      return self.session.run(names, feeds)
  monkeypatch.setattr(split, 'GpuSession', EmulatedSession)
  for side in isolated.SIDES:
    gpu = isolated.replay_isolated_conv(stage, side)
    assert all(row['gpu_vs_cpu']['passed'] for row in gpu['cases'])
    assert 'gpu_rounded_fp16_vs_original_graph_cpu' in gpu['cases'][0]
    assert all('gpu_rounded_fp16_vs_isolated_cpu_fp16' in row for row in gpu['cases'])
    assert 'gpu_rounded_fp16_vs_original_graph_cpu' not in gpu['cases'][1]
    assert gpu['production_activation_allowed'] is False
  assert len(closed) == 2
  assert (source.read_bytes(), (stage / 'weights.bin').read_bytes()) == before


def test_isolated_conv_serial_work_does_not_build_whole_gpu_graph(tmp_path, monkeypatch):
  from openpilot.tools.jetson.tests.test_nexo_n16_split_gpu import accepted, SPEC
  receipt = accepted()
  monkeypatch.setattr(split, 'load_registry', lambda: (SPEC, None))
  calls = []
  def fake(target, args, role, *a, **kw):
    calls.append(role)
    result = {'controller_and_cpu_worker_rss_bytes_sampled': 1024, 'ort_version': 'test'}
    if role == 'reference':
      result['frames'] = 1
    elif role == 'probe':
      result['plan'] = receipt['boundary_transport']
    elif role == 'trace_original':
      result['isolated_conv_cpu'] = {'cases': []}
    elif role.startswith('build_'):
      assert not list(tmp_path.glob('*.plan'))
      (tmp_path / (role.removeprefix('build_') + '.plan')).write_bytes(b'trial')
    return result
  monkeypatch.setattr(split, 'isolated_phase', fake)
  report = split.run_offline(tmp_path, tmp_path, tmp_path, receipt, lambda _: None, ('conv-isolated',))
  assert calls == ['reference', 'probe', 'trace_original', 'build_conv16', 'gpu_conv16', 'build_conv32', 'gpu_conv32']
  assert not list(tmp_path.glob('*.plan'))
  assert report['phase'] == 'offline_gpu_isolated_conv_complete'
  assert not report['gpu_inference_verified'] and not report['gpu_parity_verified'] and not report['production_activation_allowed']
  assert classify_trial({**report, 'trace_targets': ['conv-isolated']})['diagnosis_code'] == 'offline_gpu_isolated_conv_only'


def test_isolated_preset_cannot_mix_with_other_trace_targets():
  with pytest.raises(ValueError, match='trace requires'):
    trial(lambda: pytest.fail('device access'), partition_gpu_parity=True, partition_cut=360,
          trace_targets=['conv-isolated', 'conv2d'])
