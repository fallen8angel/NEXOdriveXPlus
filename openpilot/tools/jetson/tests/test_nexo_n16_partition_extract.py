"""CPU-only structural and numerical tests of N16 ONNX partition extraction."""
import copy
import json

import pytest
onnx = pytest.importorskip('onnx')
np = pytest.importorskip('numpy')
from onnx import TensorProto, helper, numpy_helper

from openpilot.tools.jetson.nexo_n16_partition_plan import inspect_graph
from openpilot.tools.jetson.nexo_n16_partition_extract import (
  _node_digest, extract_in_place, verify_exported_halves, _file_digest,
)


def sample_model(*, shared=False, early_output=False, right_direct=False):
  inp = [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 4])]
  if right_direct:
    inp.append(helper.make_tensor_value_info('nav', TensorProto.FLOAT, [1, 4]))
  keys = ['a', 'b', 'c', 'y']
  weights = [numpy_helper.from_array(np.arange(4, dtype=np.float32).reshape(1, 4),
                                      f'w{i}') for i in range(4)]
  nodes = []
  for i, key in enumerate(keys):
    parent = 'x' if i == 0 else keys[i-1]
    if i == 3 and right_direct:
      parent = 'nav'
    nodes.append(helper.make_node('Add',
                  [parent, 'w0' if shared and i == 3 else f'w{i}'],
                  [key], name='add'+str(i)))
  if right_direct:
    # Add a true boundary dependency after the independent navigation input.
    nodes[3] = helper.make_node('Add', ['c', 'nav'], ['y'], name='add3')
  output = [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1, 4])]
  if early_output:
    output.append(helper.make_tensor_value_info('b', TensorProto.FLOAT, [1, 4]))
  info = [helper.make_tensor_value_info(k, TensorProto.FLOAT, [1, 4]) for k in keys[:-1]]
  graph = helper.make_graph(nodes, 'mock-carrot', inp, output, weights, value_info=info)
  model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])
  # Synthetic unit tests target ORT 1.23 (IR <= 11); never downgrade N16.
  model.ir_version = min(model.ir_version, 11)
  return model


def extract_pair(model, directory, *, cut=2):
  plan = inspect_graph(model)
  assert plan['state'] == 'candidate_found'
  candidate = next(x for x in plan['candidates'] if x['cut_node_index'] == cut)
  directory.mkdir()
  outputs = {}
  for side in ('left', 'right'):
    original = copy.deepcopy(model)
    full = _node_digest(original.graph.node)
    part = extract_in_place(original, candidate, side)
    part['original_node_digest'] = full
    part['source_sha256'] = 'test-source'
    part['boundary_tensors'] = candidate['boundary_tensors']
    file = directory / (side + '.onnx')
    onnx.save(original, str(file))
    part['partition_file_bytes'] = file.stat().st_size
    part['partition_sha256'] = _file_digest(file)
    (directory / (side + '.json')).write_text(json.dumps(part))
    outputs[side] = original
  return outputs


def test_split_preserves_all_nodes_and_weights_and_checksums(tmp_path):
  original = sample_model()
  exported = extract_pair(original, tmp_path / 'out')
  report = verify_exported_halves(tmp_path / 'out', cut=2)
  assert report['state'] == 'models_extracted'
  assert report['node_stream_recombined_exact'] is True
  assert report['numerical_parity_verified'] is False
  assert [n.name for n in exported['left'].graph.node] == ['add0', 'add1']
  assert [n.name for n in exported['right'].graph.node] == ['add2', 'add3']
  assert [v.name for v in exported['left'].graph.output] == ['b']
  assert [v.name for v in exported['right'].graph.input] == ['b']
  assert {v.name for v in exported['left'].graph.initializer} == {'w0', 'w1'}
  assert {v.name for v in exported['right'].graph.initializer} == {'w2', 'w3'}


def test_original_output_on_left_retained(tmp_path):
  outputs = extract_pair(sample_model(early_output=True), tmp_path / 'x')
  assert 'b' in [x.name for x in outputs['left'].graph.output]
  assert verify_exported_halves(tmp_path / 'x', cut=2)['structural_verification_passed']


def test_shared_initializer_is_duplicated_exactly(tmp_path):
  exported = extract_pair(sample_model(shared=True), tmp_path / 's')
  assert 'w0' in {w.name for w in exported['right'].graph.initializer}
  assert verify_exported_halves(tmp_path / 's', cut=2)['node_stream_recombined_exact']


def test_changed_export_is_rejected(tmp_path):
  extract_pair(sample_model(), tmp_path / 'h')
  path = tmp_path / 'h' / 'right.onnx'
  with path.open('ab') as writer:
    writer.write(b'corrupted')
  with pytest.raises(ValueError, match='digest'):
    verify_exported_halves(tmp_path / 'h', cut=2)


def test_optional_cpu_numerical_equivalence_for_small_graph(tmp_path):
  ort = pytest.importorskip('onnxruntime')
  original = sample_model()
  models = extract_pair(original, tmp_path / 'numerical')
  x = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32)
  def run(m, feeds):
    sess = ort.InferenceSession(m.SerializeToString(), providers=['CPUExecutionProvider'])
    return dict(zip([o.name for o in sess.get_outputs()], sess.run(None, feeds)))
  expected = run(original, {'x': x})
  first = run(models['left'], {'x': x})
  second = run(models['right'], {'b': first['b']})
  np.testing.assert_array_equal(second['y'], expected['y'])


def test_no_unsafe_side_or_undefined_input(tmp_path):
  m = sample_model()
  plan = inspect_graph(m)
  candidate = plan['candidates'][0]
  with pytest.raises(ValueError, match='side'):
    extract_in_place(m, candidate, 'unknown')
  altered = copy.deepcopy(m)
  altered.graph.node[1].input[0] = 'does_not_exist'
  with pytest.raises(ValueError, match='Unbound|Check|Graph'):
    extract_in_place(altered, candidate, 'left')

def test_model_level_onnx_function_kept_verbatim_in_both_halves(tmp_path):
  """A legal local ONNX function is a definition, not an unsafe subgraph cut."""
  original = sample_model()
  fn = helper.make_function(
    'nexo.test', 'LocalAdd', ['A', 'B'], ['C'],
    [helper.make_node('Add', ['A', 'B'], ['C'])],
    [helper.make_opsetid('', 17)])
  original.functions.append(fn)
  original.opset_import.append(helper.make_opsetid('nexo.test', 1))
  original.graph.node[0].op_type = 'LocalAdd'
  original.graph.node[0].domain = 'nexo.test'
  onnx.checker.check_model(original, full_check=False)

  exported = extract_pair(original, tmp_path / 'functions')
  report = verify_exported_halves(tmp_path / 'functions', cut=2)
  assert report['state'] == 'models_extracted'
  assert report['original_local_function_count'] == 1
  assert report['local_functions_preserved_identical'] is True
  for side in ('left', 'right'):
    assert len(exported[side].functions) == 1
    assert exported[side].functions[0].SerializeToString() == fn.SerializeToString()
    assert ('nexo.test', 1) in {
      (v.domain, v.version) for v in exported[side].opset_import}


def test_model_level_function_tamper_rejected_by_digest(tmp_path):
  original = sample_model()
  fn = helper.make_function(
    'nexo.test', 'LocalAdd', ['A', 'B'], ['C'],
    [helper.make_node('Add', ['A', 'B'], ['C'])],
    [helper.make_opsetid('', 17)])
  original.functions.append(fn)
  original.opset_import.append(helper.make_opsetid('nexo.test', 1))
  exported = extract_pair(original, tmp_path / 'tamper_function')
  assert exported['left'].functions
  file = tmp_path / 'tamper_function' / 'right.onnx'
  changed = onnx.load(str(file))
  changed.functions[0].name = 'CorruptedFunction'
  onnx.save(changed, str(file))
  # Even a validly serialized model must not pass the saved hash receipt.
  with pytest.raises(ValueError, match='digest'):
    verify_exported_halves(tmp_path / 'tamper_function', cut=2)


def test_sparse_initializer_fails_with_explicit_diagnostic():
  original = sample_model()
  # Empty sparse initializer is enough to assert that the unsupported data
  # storage format is not silently dropped.
  sparse = onnx.SparseTensorProto()
  original.graph.sparse_initializer.append(sparse)
  candidate = inspect_graph(original)['candidates'][0]
  with pytest.raises(ValueError, match='Sparse initializers.*count=1'):
    extract_in_place(original, candidate, 'left')
