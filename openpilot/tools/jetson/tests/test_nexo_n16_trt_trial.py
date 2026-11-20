"""CPU-only invariants for isolated Carrot N16 stateful image patch."""
import pytest
from pathlib import Path

onnx = pytest.importorskip("onnx")
np = pytest.importorskip("numpy")
from onnx import TensorProto, helper, numpy_helper

from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images, prepare_onnx_separately, validate_build_profile


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


def test_n16_memory_profile_validation():
  assert validate_build_profile(None, 3) is None
  assert validate_build_profile(256, 0) == 256 * 1024 * 1024
  assert validate_build_profile(512, 1) == 512 * 1024 * 1024
  for invalid in (128, 255, 1025, -1, True, '256'):
    with pytest.raises(ValueError, match='workspace_mib'):
      validate_build_profile(invalid, 0)
  for invalid in (-1, 4, True, '0'):
    with pytest.raises(ValueError, match='optimization_level'):
      validate_build_profile(256, invalid)


def test_separate_onnx_preprocessor_keeps_graph_operations(tmp_path):
  source, target = tmp_path / 'n16-input.onnx', tmp_path / 'n16-prepatched.onnx'
  original = _sample_model()
  onnx.save(original, str(source))
  phases = []
  prepare_onnx_separately(source, target, lambda model: None, on_phase=phases.append)
  assert phases == ['onnx_load', 'onnx_retype', 'onnx_check', 'onnx_save', 'onnx_prepatch_complete']
  patched = onnx.load(str(target))
  onnx.checker.check_model(patched)
  assert [n.op_type for n in patched.graph.node] == [n.op_type for n in original.graph.node]
  assert patched.graph.input[0].type.tensor_type.elem_type == TensorProto.FLOAT16
  assert patched.graph.output[0].type.tensor_type.elem_type == TensorProto.FLOAT16


def test_separate_onnx_preprocessor_rejects_symlink(tmp_path):
  source, target = tmp_path / 'source.onnx', tmp_path / 'patched.onnx'
  real_source = tmp_path / 'actual.onnx'
  onnx.save(_sample_model(), str(real_source))
  source.symlink_to(real_source)
  with pytest.raises(ValueError, match='symlink'):
    prepare_onnx_separately(source, target, lambda model: None)
  assert not target.exists()
