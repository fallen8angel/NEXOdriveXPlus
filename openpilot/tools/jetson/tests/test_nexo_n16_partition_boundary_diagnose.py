"""Offline N16 cut error-localization tests; no Jetson, GPU, CAN or HUD."""
import copy
import json

import pytest

onnx = pytest.importorskip('onnx')
np = pytest.importorskip('numpy')
pytest.importorskip('onnxruntime')
from onnx import TensorProto, helper, numpy_helper

from openpilot.tools.jetson.nexo_n16_partition_plan import inspect_graph
from openpilot.tools.jetson.nexo_n16_partition_extract import _node_digest, extract_in_place
import openpilot.tools.jetson.nexo_n16_registry as registry
import openpilot.tools.jetson.nexo_n16_partition_boundary_diagnose as boundary
import openpilot.tools.jetson.nexo_n16_partition_cpu_parity as parity


def tiny_model():
  nodes = [helper.make_node('Add', ['x' if i == 0 else f't{i-1}', f'w{i}'],
                            ['y' if i == 3 else f't{i}'], name=f'add{i}') for i in range(4)]
  weights = [numpy_helper.from_array(np.full((1, 4), i+1, np.float32), name=f'w{i}')
             for i in range(4)]
  graph = helper.make_graph(
    nodes, 'split-probe',
    [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1,4])],
    [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1,4])],
    weights, value_info=[helper.make_tensor_value_info(f't{i}', TensorProto.FLOAT, [1,4])
                         for i in range(3)])
  model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])
  model.ir_version = min(model.ir_version, 11)
  return model


@pytest.fixture
def sample(tmp_path, monkeypatch):
  monkeypatch.setattr(registry, 'EXPECTED_INPUTS', {'x'})
  monkeypatch.setattr(registry, 'EXPECTED_OUTPUTS', {'y'})
  monkeypatch.setattr(parity, 'SLICES', {})
  monkeypatch.setattr(boundary, 'SLICES', {})
  monkeypatch.setattr(boundary, 'N16_CUT', 2)
  model = tiny_model()
  original = tmp_path / 'original.onnx'
  onnx.save_model(model, original)
  cut = next(v for v in inspect_graph(model)['candidates'] if v['cut_node_index'] == 2)
  for side in ('left', 'right'):
    part = copy.deepcopy(model)
    extract_in_place(part, cut, side)
    onnx.save_model(part, tmp_path / f'{side}.onnx')
  # This unit model has one boundary tensor; test instrument math independently
  # and mock pinned count only here, never in production.
  receipt = {'cut_node_index': 2,
             'original_node_count': len(model.graph.node),
             'original_node_digest': _node_digest(model.graph.node),
             'boundary_tensors': cut['boundary_tensors']}
  receipt_path = tmp_path / 'left.json'
  receipt_path.write_text(json.dumps(receipt))
  return original, receipt_path, cut['boundary_tensors']


def test_probe_exposes_variable_cut_tensors_without_changing_operations(
    sample, tmp_path, monkeypatch):
  original, receipt_path, boundary_tensors = sample
  assert len(boundary_tensors) == 1
  monkeypatch.setattr(boundary, 'MIN_PROBE_BYTES', 0)
  probe = tmp_path / 'probe.onnx'
  info = boundary.instrument_reference(original, probe, receipt_path, cut=2)
  assert info['added_graph_outputs'] == len(boundary_tensors)
  assert info['cut_node_index'] == 2
  assert info['original_math_unchanged']
  checked = onnx.load(str(probe))
  assert _node_digest(checked.graph.node) == _node_digest(onnx.load(str(original)).graph.node)
  assert {value.name for value in checked.graph.output} == (
      {'y'} | {item['name'] for item in boundary_tensors})
  with pytest.raises(ValueError, match='cut tensor contract'):
    boundary.instrument_reference(original, tmp_path / 'wrong-cut.onnx',
                                  receipt_path, cut=3)
  assert not (tmp_path / 'wrong-cut.onnx').exists()


def test_output_metrics_classify_original_probe_change_as_inconclusive(tmp_path, monkeypatch):
  monkeypatch.setattr(boundary, 'SLICES', {})
  names = [f'bridge{i}' for i in range(8)]
  baseline = {'y': np.array([[1,2,3]], dtype=np.float32)}
  for case in range(2):
    parity.save_arrays(tmp_path / f'reference-{case}.npz', baseline)
    parity.save_arrays(tmp_path / f'probe-reference-{case}.npz',
                       {'y': baseline['y'] + 0.04})
    original_boundary = {name: np.array([[i]], dtype=np.float32)
                         for i,name in enumerate(names)}
    parity.save_arrays(tmp_path / f'reference-boundary-{case}.npz', original_boundary)
    parity.save_arrays(tmp_path / f'left-{case}.npz', original_boundary)
    parity.save_arrays(tmp_path / f'right-{case}.npz', baseline)
    parity.save_arrays(tmp_path / f'right_reference-{case}.npz', baseline)
  receipt = boundary.compare_boundary_diagnosis(tmp_path, {'y':[1,3]}, names)
  assert receipt['state'] == 'boundary_diagnosis_complete'
  assert receipt['candidate_location'] == 'inconclusive_instrumented_original_changed_outputs'
  assert receipt['reference_probe_unchanged'] is False
  assert receipt['numerical_parity_verified'] is False
  assert receipt['vehicle_control_enabled'] is False


def test_upstream_cut_mismatch_is_localized_without_relaxing_tolerance(tmp_path, monkeypatch):
  monkeypatch.setattr(boundary, 'SLICES', {})
  names = [f'bridge{i}' for i in range(8)]
  baseline = {'y': np.array([[1,2,3]], dtype=np.float32)}
  for case in range(2):
    parity.save_arrays(tmp_path / f'reference-{case}.npz', baseline)
    parity.save_arrays(tmp_path / f'probe-reference-{case}.npz', baseline)
    original_boundary = {name: np.array([[i]], dtype=np.float32)
                         for i,name in enumerate(names)}
    left = {name: x.copy() for name,x in original_boundary.items()}
    left[names[3]][0, 0] += 0.1
    parity.save_arrays(tmp_path / f'reference-boundary-{case}.npz', original_boundary)
    parity.save_arrays(tmp_path / f'left-{case}.npz', left)
    parity.save_arrays(tmp_path / f'right-{case}.npz',
                       {'y': baseline['y'] + 0.1})
    parity.save_arrays(tmp_path / f'right_reference-{case}.npz', baseline)
  receipt = boundary.compare_boundary_diagnosis(tmp_path, {'y':[1,3]}, names)
  assert receipt['candidate_location'] == 'left_half_or_boundary_output_materialization'
  assert not receipt['left_boundary_agrees']
  assert receipt['right_using_true_boundary_agrees']
  assert not receipt['full_split_chain_agrees']
  assert all(row['left_boundary']['failed_tensors'] == [names[3]]
             for row in receipt['cases'])
  assert receipt['numerical_parity_verified'] is False


def test_downstream_mismatch_with_true_reference_boundary_is_localized(tmp_path, monkeypatch):
  monkeypatch.setattr(boundary, 'SLICES', {})
  names = [f'bridge{i}' for i in range(8)]
  baseline = {'y': np.array([[1,2,3]], dtype=np.float32)}
  for case in range(2):
    parity.save_arrays(tmp_path / f'reference-{case}.npz', baseline)
    parity.save_arrays(tmp_path / f'probe-reference-{case}.npz', baseline)
    bridge = {name: np.array([[i]], dtype=np.float32) for i,name in enumerate(names)}
    parity.save_arrays(tmp_path / f'reference-boundary-{case}.npz', bridge)
    parity.save_arrays(tmp_path / f'left-{case}.npz', bridge)
    parity.save_arrays(tmp_path / f'right-{case}.npz',
                       {'y': baseline['y'] + 0.1})
    parity.save_arrays(tmp_path / f'right_reference-{case}.npz',
                       {'y': baseline['y'] + 0.1})
  receipt = boundary.compare_boundary_diagnosis(tmp_path, {'y':[1,3]}, names)
  assert receipt['candidate_location'] == 'right_half_or_boundary_input_materialization'
  assert receipt['left_boundary_agrees']
  assert not receipt['right_using_true_boundary_agrees']
  assert receipt['numerical_parity_verified'] is False


def test_nine_real_boundary_tensors_execute_original_and_two_splits(
    tmp_path, monkeypatch):
  """End-to-end CPU proof of instrumentation, passthrough and isolation."""
  import openpilot.tools.jetson.nexo_n16_partition_boundary_diagnose as diag
  import openpilot.tools.jetson.nexo_n16_partition_cpu_parity as cpu
  monkeypatch.setattr(diag, 'N16_CUT', 9)
  monkeypatch.setattr(diag, 'MIN_PROBE_BYTES', 0)
  monkeypatch.setattr(diag, 'SLICES', {})
  monkeypatch.setattr(cpu, 'SLICES', {})
  monkeypatch.setattr(registry, 'EXPECTED_INPUTS', {'x'})
  monkeypatch.setattr(registry, 'EXPECTED_OUTPUTS', {'y'})
  nodes = [helper.make_node('Add', ['x', f'w{i}'], [f't{i}']) for i in range(9)]
  nodes.append(helper.make_node('Sum', [f't{i}' for i in range(9)], ['a0']))
  nodes.extend(helper.make_node('Add', ['a0' if i == 9 else f'a{i-8}', f'w{i}'],
                                [f'a{i-7}' if i < 11 else 'y']) for i in range(9, 12))
  # Right half has three initializer weights and the first nine comprise
  # exact graph boundary tensors; no N16 production shapes are assumed.
  weights = [numpy_helper.from_array(np.ones((1,4), dtype=np.float32)*(i+1),
                                     f'w{i}') for i in range(12)]
  graph = helper.make_graph(
    nodes, 'nine-cut-example',
    [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1,4])],
    [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1,4])],
    initializer=weights,
    value_info=[helper.make_tensor_value_info(f't{i}', TensorProto.FLOAT, [1,4])
                for i in range(9)] +
               [helper.make_tensor_value_info(f'a{i}', TensorProto.FLOAT, [1,4])
                for i in range(4)])
  model = helper.make_model(graph, opset_imports=[helper.make_opsetid('',17)])
  model.ir_version = min(model.ir_version, 11)
  original = tmp_path / 'original.onnx'
  onnx.save_model(model, str(original))
  bridge = [{'name': f't{i}', 'dtype': TensorProto.FLOAT,
             'shape':[1,4], 'nbytes':16} for i in range(9)]
  cut = {'cut_node_index':9, 'boundary_tensors':bridge,
         'left_inputs':['x'], 'left_outputs':[f't{i}' for i in range(9)],
         'right_inputs':[f't{i}' for i in range(9)], 'right_outputs':['y']}
  for side in ('left','right'):
    part = copy.deepcopy(model)
    extract_in_place(part, cut, side)
    onnx.save_model(part, str(tmp_path / (side+'.onnx')))
  receipt = {'cut_node_index':9, 'original_node_count':len(model.graph.node),
             'original_node_digest':_node_digest(model.graph.node),
             'boundary_tensors':bridge}
  (tmp_path / 'left.json').write_text(json.dumps(receipt))
  info = diag.instrument_reference(original, tmp_path / 'probe.onnx',
                                   tmp_path / 'left.json', cut=9)
  (tmp_path / 'probe-boundary.json').write_text(json.dumps(info))
  cpu.cpu_step('reference', original, tmp_path)
  cpu.cpu_step('reference_probe', tmp_path / 'probe.onnx', tmp_path)
  cpu.cpu_step('left', tmp_path / 'left.onnx', tmp_path)
  cpu.cpu_step('right', tmp_path / 'right.onnx', tmp_path)
  cpu.cpu_step('right_reference', tmp_path / 'right.onnx', tmp_path)
  score = diag.compare_boundary_diagnosis(tmp_path, {'y':[1,4]},
                                           [f't{i}' for i in range(9)])
  assert score['state'] == 'boundary_diagnosis_complete'
  assert score['candidate_location'] == 'no_difference_in_two_synthetic_cases'
  assert score['reference_probe_unchanged']
  assert score['left_boundary_agrees']
  assert score['right_using_true_boundary_agrees']
  assert score['full_split_chain_agrees']
  assert score['numerical_parity_verified'] is False
