"""CPU provider graph extraction and first-frame fault localization."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from onnx import helper

from openpilot.tools.jetson.nexo_n16_cpu_plan import check_plan_space, probe_first_frame, source_contract, split_plan
from openpilot.tools.jetson.nexo_n16_cpu_precision import compare_sequence, cpu_session
from openpilot.tools.jetson.nexo_n16_cpu_workers import write_reference
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import load_arrays
from openpilot.tools.jetson.tests.test_nexo_n16_cpu_precision import graphs, SPEC


def prepare(stage, *, stateful=True):
  model, left, right, tensors = graphs(stateful)
  # The old unconditional widening has a known counterexample when a pair
  # of layout ops inside the full graph materializes half precision.
  for index, source, target in ((0, 'bridge', 'layout1'), (1, 'layout1', 'layout')):
    model.graph.node.insert(len(left.graph.node) + index, helper.make_node(
      'Transpose', [source], [target], perm=[1, 0]))
  model.graph.node[len(left.graph.node) + 2].input[0] = 'layout'
  for index, node in enumerate(model.graph.node):
    node.name = 'node_' + str(index)
  fields = {side + '_' + key: [v.name for v in getattr(part.graph, attr)] for side, part in
            [('left', left), ('right', right)] for key, attr in [('inputs', 'input'), ('outputs', 'output')]}
  contract = source_contract(model, {'boundary_tensors': tensors, **fields})
  (stage / 'contract.json').write_text(json.dumps(contract))
  original = cpu_session(model, save_plan=stage / 'execution.onnx')
  return original, contract


def test_resolved_graph_preserves_original_rounding_on_transpose_counterexample(tmp_path):
  original, contract = prepare(tmp_path, stateful=False)
  proof = split_plan(tmp_path, contract)
  lhs, rhs = (cpu_session(tmp_path / (side + '.onnx')) for side in ('left', 'right'))
  edge = proof['boundary'][0]['tensor']
  for seed in range(8):
    feeds = {'x': np.random.default_rng(seed).uniform(-2, 2, (1, 64)).astype('float16')}
    expected = original.run(None, feeds)[0]
    actual = rhs.run(None, {edge: lhs.run(None, feeds)[0]})[0]
    np.testing.assert_array_equal(actual, expected)


def test_recorded_probe_and_all_recurrent_frames_with_boundary_damage(tmp_path):
  original, _ = prepare(tmp_path)
  spec = {**SPEC, 'sha256': 'f' * 64, 'input_shapes': {i.name: i.shape for i in original.get_inputs()}}
  write_reference(original, spec, tmp_path / 'reference.xz', lambda _: None)
  proof = probe_first_frame(tmp_path, spec)
  left, right = (cpu_session(tmp_path / (side + '.onnx')) for side in ('left', 'right'))
  boundary = load_arrays(tmp_path / 'first-boundary.npz')
  good = compare_sequence(original, left, right, spec, first_boundary=boundary)
  assert proof['probe_public_outputs_exact'] and proof['source_initializers_verified'] == 2
  assert good['stateful_140_frames_verified'] and max(good['first_frame_boundary_max_abs_error'].values()) == 0
  edge = proof['boundary'][0]['tensor']
  boundary[edge].flat[0] += 0.25
  bad = compare_sequence(original, left, right, spec, first_boundary=boundary)
  assert bad['state'] == 'cpu_parity_failed' and bad['frames_completed'] == 140
  assert bad['failed_outputs'][0]['output'] == 'boundary:' + edge


def test_missing_producer_or_changed_weight_rejected(tmp_path):
  _, contract = prepare(tmp_path)
  name = next(iter(contract['ports']))
  contract['ports'][name][0] = 'missing'
  with pytest.raises(ValueError, match='identify CPU boundary'):
    split_plan(tmp_path, contract)
  contract['weights']['w'][2] = '0' * 64
  with pytest.raises(ValueError, match='initializer mismatch'):
    split_plan(tmp_path, contract)


def test_compact_plan_reserves_space_for_weights_journal_and_disk_floor(tmp_path, monkeypatch):
  import openpilot.tools.jetson.nexo_n16_cpu_plan as plan
  monkeypatch.setattr(plan.shutil, 'disk_usage', lambda _: SimpleNamespace(free=2 << 30))
  with pytest.raises(RuntimeError, match='storage reserve'):
    check_plan_space(tmp_path, 815000000)
