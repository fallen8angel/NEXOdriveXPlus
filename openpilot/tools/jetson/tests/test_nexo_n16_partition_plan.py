"""No GPU required: verify the structural N16 cut planner fails closed."""
import pytest

onnx = pytest.importorskip('onnx')
np = pytest.importorskip('numpy')
from onnx import TensorProto, helper, numpy_helper
from openpilot.tools.jetson.nexo_n16_partition_plan import inspect_graph


def model(*, shapes=True, nested=False, shared=False):
  keys = ['a', 'b', 'c', 'y']
  weights = [numpy_helper.from_array(np.ones((1, 4), dtype=np.float16), f'w{i}')
             for i in range(4)]
  nodes = [helper.make_node('Add', ['x' if i == 0 else keys[i-1],
                                    'w0' if shared and i == 3 else f'w{i}'],
                            [key], name=f'add{i}') for i, key in enumerate(keys)]
  if nested:
    graph = helper.make_graph([], 'branch', [], [])
    nodes[0].attribute.append(helper.make_attribute('branch', graph))
  vi = [helper.make_tensor_value_info(x, TensorProto.FLOAT16, [1, 4])
        for x in keys[:-1]] if shapes else []
  g = helper.make_graph(nodes, 'chain',
                        [helper.make_tensor_value_info('x', TensorProto.FLOAT16, [1, 4])],
                        [helper.make_tensor_value_info('y', TensorProto.FLOAT16, [1, 4])],
                        weights, value_info=vi)
  return helper.make_model(g, opset_imports=[helper.make_opsetid('', 17)])


def test_narrow_balanced_cut_preserves_boundary_contract():
  plan = inspect_graph(model())
  assert plan['state'] == 'candidate_found'
  assert plan['gpu_inference_verified'] is False
  assert plan['engine_build_attempted'] is False
  best = plan['candidates'][0]
  assert best['cut_node_index'] == 2
  assert best['left_inputs'] == ['x']
  assert best['left_outputs'] == ['b']
  assert best['right_inputs'] == ['b']
  assert best['right_outputs'] == ['y']
  assert best['boundary_tensors'][0]['shape'] == [1, 4]
  assert best['boundary_bytes_estimate'] == 8


def test_missing_shapes_do_not_estimate_unknown_tensors():
  plan = inspect_graph(model(shapes=False))
  assert plan['state'] == 'blocked'
  assert not plan['candidates']


def test_control_flow_refused():
  plan = inspect_graph(model(nested=True))
  assert plan['state'] == 'blocked'
  assert 'Nested' in plan['reason']


def test_shared_weights_report_duplication():
  plan = inspect_graph(model(shared=True))
  assert plan['state'] == 'candidate_found'
  assert any(c['duplicated_weight_bytes_estimate'] >= 8
             for c in plan['candidates'])


def test_unknown_input_cannot_be_used():
  sample = model()
  sample.graph.node[2].input[0] = 'unknown'
  plan = inspect_graph(sample)
  assert plan['state'] == 'blocked'
  assert 'topologically' in plan['reason']


def test_weightless_model_rejected():
  nodes = [helper.make_node('Identity', [x], [y]) for x, y in
           zip(('x', 'a', 'b', 'c'), ('a', 'b', 'c', 'y'))]
  g = helper.make_graph(
    nodes, 'no-weights',
    [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1])],
    [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1])],
    value_info=[helper.make_tensor_value_info(x, TensorProto.FLOAT, [1])
                for x in ('a', 'b', 'c')])
  plan = inspect_graph(helper.make_model(g))
  assert plan['state'] == 'blocked'
  assert 'weights' in plan['reason']
