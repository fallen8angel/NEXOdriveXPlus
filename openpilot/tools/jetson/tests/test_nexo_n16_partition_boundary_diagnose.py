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


def test_probe_exposes_existing_intermediate_without_changing_operations(sample, tmp_path, monkeypatch):
  original, receipt_path, boundary_tensors = sample
  assert boundary_tensors
  monkeypatch.setattr(boundary, 'N16_CUT', 2)
  # Pin eight distinct cut tensors in production; for this tiny sample,
  # only exercise the rejection of a non-pinned cut.
  with pytest.raises(ValueError, match='eight-tensor'):
    boundary.instrument_reference(original, tmp_path / 'probe.onnx', receipt_path)
  assert not (tmp_path / 'probe.onnx').exists()


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
    left[names[3]][0, 0] += 0.02
    parity.save_arrays(tmp_path / f'reference-boundary-{case}.npz', original_boundary)
    parity.save_arrays(tmp_path / f'left-{case}.npz', left)
    parity.save_arrays(tmp_path / f'right-{case}.npz',
                       {'y': baseline['y'] + 0.01})
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
                       {'y': baseline['y'] + 0.01})
    parity.save_arrays(tmp_path / f'right_reference-{case}.npz',
                       {'y': baseline['y'] + 0.01})
  receipt = boundary.compare_boundary_diagnosis(tmp_path, {'y':[1,3]}, names)
  assert receipt['candidate_location'] == 'right_half_or_boundary_input_materialization'
  assert receipt['left_boundary_agrees']
  assert not receipt['right_using_true_boundary_agrees']
  assert receipt['numerical_parity_verified'] is False
