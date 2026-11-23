"""Actual CPU FP16 rounding regression, independent recurrence and strict acceptance."""
import copy
from types import SimpleNamespace

import numpy as np
import pytest
from onnx import TensorProto as T, helper as h, numpy_helper as n

from openpilot.tools.jetson.nexo_n16_cpu_precision import (
  compare_sequence, cpu_session, image_adapter, run_stateful_cpu, widen_boundary,
)
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import compare_array


def graphs(stateful=False):
  rng = np.random.default_rng(2)
  shape = [1, 64]
  weights = [n.from_array(rng.uniform(-2, 2, (64, 64)).astype('float16'), 'w'),
             n.from_array(rng.uniform(-2, 2, shape).astype('float16'), 'b')]
  nodes = [h.make_node('MatMul', ['x', 'w'], ['a']), h.make_node('Add', ['a', 'b'], ['bridge']),
           h.make_node('MatMul', ['bridge', 'w'], ['outputs'])]
  inputs = [h.make_tensor_value_info('x', T.FLOAT16, shape)]
  outputs = [h.make_tensor_value_info('outputs', T.FLOAT16, shape)]
  if stateful:
    weights = [n.from_array(np.eye(64, dtype=np.float16) * 0.125, 'w'),
               n.from_array(np.full(shape, 0.03125, dtype=np.float16), 'b')]
    nodes.insert(0, h.make_node('Cast', ['new_img'], ['image'], to=T.FLOAT16))
    nodes.insert(1, h.make_node('Add', ['image', 'state_feat_q'], ['x']))
    nodes.append(h.make_node('Identity', ['outputs'], ['next_state_feat_q']))
    inputs = [h.make_tensor_value_info('new_img', T.UINT8, shape),
              h.make_tensor_value_info('state_feat_q', T.FLOAT16, shape)]
    outputs.append(h.make_tensor_value_info('next_state_feat_q', T.FLOAT16, shape))
  graph = h.make_graph(nodes, 'rounding', inputs, outputs, weights)
  model = h.make_model(graph, opset_imports=[h.make_opsetid('', 17)])
  model.ir_version = 10
  cut = 4 if stateful else 2
  left, right = copy.deepcopy(model), copy.deepcopy(model)
  del left.graph.node[cut:]
  del left.graph.output[:]
  left.graph.output.append(h.make_tensor_value_info('bridge', T.FLOAT16, shape))
  del right.graph.node[:cut]
  del right.graph.input[:]
  right.graph.input.append(h.make_tensor_value_info('bridge', T.FLOAT16, shape))
  for index in reversed(range(len(right.graph.initializer))):
    if right.graph.initializer[index].name == 'b':
      del right.graph.initializer[index]
  tensors = [{'name': 'bridge', 'dtype': T.FLOAT16, 'shape': shape}]
  return model, left, right, tensors


def test_real_cpu_fp16_split_rounding_is_fixed_without_changing_weights_or_public_io():
  original, left, right, tensors = graphs()
  feeds = {'x': np.random.default_rng(2).uniform(-2, 2, (1, 64)).astype('float16')}
  expected = cpu_session(original).run(None, feeds)[0]
  native = cpu_session(right).run(None, {'bridge': cpu_session(left).run(None, feeds)[0]})[0]
  assert not compare_array(expected, native, 'outputs')['passed']
  for side, model in (('left', left), ('right', right)):
    weights = [value.SerializeToString() for value in model.graph.initializer]
    functions = [value.SerializeToString() for value in model.functions]
    imports = [value.SerializeToString() for value in model.opset_import]
    widen_boundary(model, tensors, side)
    assert weights == [value.SerializeToString() for value in model.graph.initializer]
    assert functions == [value.SerializeToString() for value in model.functions]
    assert imports == [value.SerializeToString() for value in model.opset_import]
  actual = cpu_session(right).run(None, {'bridge': cpu_session(left).run(None, feeds)[0]})[0]
  np.testing.assert_array_equal(actual, expected)
  assert original.graph.input[0].type.tensor_type.elem_type == T.FLOAT16
  assert right.graph.output[0].type == original.graph.output[0].type


def test_absolute_error_cannot_hide_behind_large_reference_values():
  metric = compare_array(np.array([1000.0], np.float32), np.array([1000.1], np.float32), 'large')
  assert not metric['passed'] and metric['outside_tolerance'] == 1


def test_precision_is_retained_when_right_half_starts_with_transpose():
  original, left, right, tensors = graphs()
  for graph, index in ((original.graph, 2), (right.graph, 0)):
    graph.node.insert(index, h.make_node('Transpose', ['bridge'], ['layout'], perm=[0, 1]))
    graph.node[index + 1].input[0] = 'layout'
  widen_boundary(left, tensors, 'left')
  receipt = widen_boundary(right, tensors, 'right')
  assert receipt['layout_cast_moves'] == 1
  assert right.graph.node[0].op_type == 'Transpose'
  assert right.graph.node[1].op_type == 'Cast'
  feeds = {'x': np.random.default_rng(2).uniform(-2, 2, (1, 64)).astype('float16')}
  expected = cpu_session(original).run(None, feeds)[0]
  actual = cpu_session(right).run(None, {'bridge': cpu_session(left).run(None, feeds)[0]})[0]
  np.testing.assert_array_equal(actual, expected)


def test_layout_transport_is_not_accepted_without_comparison_to_raw_original():
  # Some other graphs intentionally materialize FP16 at a layout operation.
  # Preserve this counterexample: casts/checker success cannot grant parity.
  original, left, right, tensors = graphs()
  for graph, index in ((original.graph, 2), (right.graph, 0)):
    graph.node.insert(index, h.make_node('Transpose', ['bridge'], ['layout1'], perm=[1, 0]))
    graph.node.insert(index + 1, h.make_node('Transpose', ['layout1'], ['layout'], perm=[1, 0]))
    graph.node[index + 2].input[0] = 'layout'
  widen_boundary(left, tensors, 'left')
  widen_boundary(right, tensors, 'right')
  feeds = {'x': np.random.default_rng(2).uniform(-2, 2, (1, 64)).astype('float16')}
  expected = cpu_session(original).run(None, feeds)[0]
  actual = cpu_session(right).run(None, {'bridge': cpu_session(left).run(None, feeds)[0]})[0]
  assert not compare_array(expected, actual, 'outputs')['passed']


def test_layout_cast_never_moves_past_public_outputs():
  _, _, right, tensors = graphs()
  right.graph.node.insert(0, h.make_node('Transpose', ['bridge'], ['public_layout'], perm=[1, 0]))
  right.graph.output.append(h.make_tensor_value_info('public_layout', T.FLOAT16, [64, 1]))
  receipt = widen_boundary(right, tensors, 'right')
  assert receipt['layout_cast_moves'] == 0
  assert right.graph.output[-1].type.tensor_type.elem_type == T.FLOAT16


@pytest.mark.parametrize('bad', [np.array([0.5], np.float16), np.array([256], np.float16),
                                 np.array([np.nan], np.float16), np.array([0], np.float32)])
def test_image_adapter_refuses_rounding_or_other_dtype(bad):
  with pytest.raises(ValueError):
    image_adapter(bad, np.dtype('uint8'))


def sequence_sessions():
  original, left, right, tensors = graphs(stateful=True)
  widen_boundary(left, tensors, 'left')
  widen_boundary(right, tensors, 'right')
  return tuple(cpu_session(model) for model in (original, left, right))


SPEC = {'output_shapes': {'outputs': [1, 64], 'next_state_feat_q': [1, 64]},
        'output_slices': {'lane_lines': [0, 32], 'plan': [32, 64]}}


def test_140_real_cpu_frames_with_independent_recurrent_state():
  result = compare_sequence(*sequence_sessions(), SPEC)
  assert result['frames_completed'] == 140 and result['stateful_140_frames_verified']
  assert result['distinct_image_frames'] == 140
  assert max(result['max_abs_error_by_output'].values()) == 0
  assert result['gpu_inference_verified'] is False and result['vehicle_control_enabled'] is False


def test_short_run_is_not_140_frame_acceptance():
  result = compare_sequence(*sequence_sessions(), SPEC, frames=2)
  assert result['state'] == 'cpu_parity_failed' and not result['cpu_numerical_parity_verified']


def test_bad_candidate_state_is_carried_independently_and_no_output_is_excluded():
  reference, left, right = sequence_sessions()
  inputs_seen = []
  def wrong_right(outputs, feeds):
    result = right.run(outputs, feeds)
    result[1] = result[1] + np.float16(0.25)
    return result
  def observed_left(outputs, feeds):
    inputs_seen.append(feeds['state_feat_q'].copy())
    return left.run(outputs, feeds)
  bad_right = SimpleNamespace(get_inputs=right.get_inputs, get_outputs=right.get_outputs, run=wrong_right)
  observed = SimpleNamespace(get_inputs=left.get_inputs, get_outputs=left.get_outputs, run=observed_left)
  result = compare_sequence(reference, observed, bad_right, SPEC, frames=3)
  assert not result['cpu_numerical_parity_verified'] and result['failed_output_count'] > 0
  assert np.all(inputs_seen[1] >= 0.25)
  assert any(row['output'] == 'next_state_feat_q' for row in result['failed_outputs'])


def test_guard_rejects_tensor_rt_options_or_multiple_cpu_modes_before_readiness():
  from openpilot.tools.jetson.nexo_n16_trt_trial import trial
  for extra in ({'workspace_mib': 256}, {'split_builder': True}, {'partition_parity_only': True}):
    with pytest.raises(ValueError):
      trial(lambda: pytest.fail('No device access'), partition_stateful_cpu_only=True, **extra)


def test_changed_source_fails_before_loading_cpu_or_gpu(tmp_path):
  source = tmp_path / 'wrong.onnx'
  source.write_bytes(b'not N16')
  with pytest.raises(ValueError, match='source identity'):
    run_stateful_cpu(source, tmp_path, 360, lambda phase: None)


def test_transport_refuses_repeated_application_and_name_collisions():
  _, left, _, tensors = graphs()
  widen_boundary(left, tensors, 'left')
  with pytest.raises(ValueError, match='verified FP16'):
    widen_boundary(left, tensors, 'left')


def test_guarded_worker_returns_from_cpu_branch_without_importing_trt(tmp_path, monkeypatch):
  import builtins
  import json
  import openpilot.tools.jetson.nexo_n16_cpu_precision as precision
  import openpilot.tools.jetson.nexo_n16_trt_trial as trial
  real_import = builtins.__import__
  def no_gpu(name, *args, **kwargs):
    if name == 'tensorrt' or name.startswith('jetlink.server.backends.trt'):
      pytest.fail('GPU import during CPU-only validation')
    return real_import(name, *args, **kwargs)
  monkeypatch.setattr(builtins, '__import__', no_gpu)
  monkeypatch.setattr(trial.os, 'setsid', lambda: None)
  monkeypatch.setattr(trial, 'digest', lambda path: trial.MODEL_SHA)
  def cpu(source, release, cut, phase, *, stage):
    assert source == trial.N16_SOURCE and cut == 360
    phase('stateful_cpu_frame_139')
    return {'state': 'cpu_parity_passed', 'frames_completed': 140}
  monkeypatch.setattr(precision, 'run_stateful_cpu', cpu)
  trial._worker(tmp_path, tmp_path, 3, None, 3, False, None,
                partition_cut=360, partition_stateful_cpu_only=True)
  report = json.loads((tmp_path / 'result.json').read_text())
  assert report['state'] == 'cpu_parity_passed'
  assert not report['engine_build_attempted'] and not report['gpu_inference_verified']


def test_unified_diagnosis_reads_existing_result_without_executing_models(tmp_path):
  import json
  from openpilot.tools.jetson.manage import n16_partition_parity_status, n16_boundary_diagnosis_status
  cache = tmp_path / 'cache'
  cache.mkdir()
  result = {'state': 'cpu_parity_passed', 'frames_completed': 140,
            'stateful_140_frames_verified': True, 'cpu_numerical_parity_verified': True,
            'failed_output_count': 0}
  path = cache / 'nexo-n16-stateful-cpu-parity-result.json'
  path.write_text(json.dumps(result))
  assert n16_partition_parity_status(tmp_path)['stateful_cpu_validation']['verified']
  result['frames_completed'] = 139
  path.write_text(json.dumps(result))
  assert not n16_partition_parity_status(tmp_path)['stateful_cpu_validation']['verified']
  (cache / 'nexo-n16-partition-boundary-diagnosis-result.json').write_text(json.dumps({
    'state': 'boundary_diagnosis_complete', 'boundary_tensor_count': 9, 'cut_node_index': 360}))
  assert n16_boundary_diagnosis_status(tmp_path)['diagnosis_completed']
