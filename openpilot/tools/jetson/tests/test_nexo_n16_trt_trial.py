"""CPU-only invariants for isolated Carrot N16 stateful image patch."""
import pytest

onnx = pytest.importorskip("onnx")
np = pytest.importorskip("numpy")
from onnx import TensorProto, helper, numpy_helper

from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images


def _sample_model(op="Concat", state_dtype=TensorProto.UINT8):
  graph = helper.make_graph(
    [
      helper.make_node("Slice", ["state_img_q", "starts", "ends", "axes", "steps"],
                       ["old_frame"], name="slice"),
      helper.make_node("Unsqueeze", ["new_img", "unsqueeze_axes"], ["new_frame"], name="unsqueeze"),
      helper.make_node(op, ["old_frame", "new_frame"], ["next_state_img_q"], name="join", axis=1)
    ],
    "stateful-image-queue",
    [
      helper.make_tensor_value_info("new_img", TensorProto.UINT8, [1, 3]),
      helper.make_tensor_value_info("state_img_q", state_dtype, [1, 2, 3]),
    ],
    [helper.make_tensor_value_info("next_state_img_q", TensorProto.UINT8, [1, 2, 3])],
    initializer=[
      numpy_helper.from_array(np.array([1], np.int64), name="starts"),
      numpy_helper.from_array(np.array([2], np.int64), name="ends"),
      numpy_helper.from_array(np.array([1], np.int64), name="axes"),
      numpy_helper.from_array(np.array([1], np.int64), name="steps"),
      numpy_helper.from_array(np.array([1], np.int64), name="unsqueeze_axes"),
    ],
    value_info=[
      helper.make_tensor_value_info("old_frame", TensorProto.UINT8, [1, 1, 3]),
      helper.make_tensor_value_info("new_frame", TensorProto.UINT8, [1, 1, 3]),
    ],
  )
  return helper.make_model(graph, opset_imports=[helper.make_opsetid("", 20)])


def test_n16_byte_preserving_queue_promotes_only_image_tensors():
  model = _sample_model()
  before = [(node.name, node.op_type, list(node.input), list(node.output)) for node in model.graph.node]
  patch_stateful_images(model)
  onnx.checker.check_model(model)
  after = [(node.name, node.op_type, list(node.input), list(node.output)) for node in model.graph.node]
  assert before == after
  fields = {item.name: item.type.tensor_type.elem_type for item in
            [*model.graph.input, *model.graph.value_info, *model.graph.output]}
  assert all(fields[name] == TensorProto.FLOAT16 for name in (
    "new_img", "state_img_q", "old_frame", "new_frame", "next_state_img_q"
  ))


def test_n16_patch_refuses_unknown_image_arithmetic():
  model = _sample_model(op="Add")
  with pytest.raises(ValueError, match="Unsupported image queue operation"):
    patch_stateful_images(model)


def test_n16_patch_refuses_unexpected_input_type():
  model = _sample_model(state_dtype=TensorProto.FLOAT16)
  with pytest.raises(ValueError, match="Expected UINT8"):
    patch_stateful_images(model)


def test_n16_patch_refuses_undocumented_image_domain():
  model = _sample_model()
  model.graph.node[0].domain = "custom.unsafe"
  with pytest.raises(ValueError, match="Unknown image queue domain"):
    patch_stateful_images(model)
