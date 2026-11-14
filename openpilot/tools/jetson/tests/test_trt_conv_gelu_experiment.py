"""Regression guards for the opt-in Conv/GELU diagnostic experiment."""
import ast
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "trt_conv_gelu_experiment.py"


class ExperimentGuards(unittest.TestCase):
  def test_syntax(self):
    ast.parse(SOURCE.read_text(encoding="utf-8"))

  def test_requires_signed_model_and_new_destination(self):
    text = SOURCE.read_text(encoding="utf-8")
    self.assertIn('hashlib.sha256(source.read_bytes()).hexdigest() != MODEL_SHA', text)
    self.assertIn('destination.exists() or destination.is_symlink()', text)
    self.assertIn('Refusing to write under the live Jetson runtime', text)

  def test_trt_ready_is_explicit_opt_in(self):
    text = SOURCE.read_text(encoding="utf-8")
    self.assertIn('parser.add_argument("--trt-ready", action="store_true"', text)
    self.assertIn("if trt_ready:", text)
    self.assertIn("retype_bridged_images_for_trt(model)", text)

  @unittest.skipUnless(importlib.util.find_spec("onnx"), "onnx not installed in this test environment")
  def test_bridged_uint8_images_retype_and_reject_ambiguous_graph(self):
    import onnx
    from onnx import TensorProto, helper

    spec = importlib.util.spec_from_file_location("nexo_gelu_experiment", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def make_graph():
      ins = [helper.make_tensor_value_info(n, TensorProto.UINT8, [1, 2])
             for n in ("img", "big_img")]
      nodes = []
      info = []
      for source, name, mid in (("img", "bridge_img", "vision/img"),
                                ("big_img", "bridge_big_img", "vision/big_img")):
        nodes.append(helper.make_node("Identity", [source], [mid], name=name))
        nodes.append(helper.make_node("Cast", [mid], [mid + "_cast"],
                                      to=TensorProto.FLOAT16, name=name + "_cast"))
        info.append(helper.make_tensor_value_info(mid, TensorProto.UINT8, [1, 2]))
      outputs = [helper.make_tensor_value_info("vision/img_cast", TensorProto.FLOAT16, [1, 2]),
                 helper.make_tensor_value_info("vision/big_img_cast", TensorProto.FLOAT16, [1, 2])]
      graph = helper.make_graph(nodes, "test", ins, outputs, value_info=info)
      return helper.make_model(graph, opset_imports=[helper.make_opsetid("", 20)])

    model = make_graph()
    module.retype_bridged_images_for_trt(model)
    onnx.checker.check_model(model)
    self.assertEqual([i.type.tensor_type.elem_type for i in model.graph.input],
                     [TensorProto.FLOAT16, TensorProto.FLOAT16])
    self.assertEqual([i.type.tensor_type.elem_type for i in model.graph.value_info],
                     [TensorProto.FLOAT16, TensorProto.FLOAT16])
    self.assertEqual([n.op_type for n in model.graph.node],
                     ["Identity", "Cast", "Identity", "Cast"])

    # The real signed combined model may omit an optional Identity
    # value_info record entirely. The graph must still be safely retyped.
    missing = make_graph()
    del missing.graph.value_info[:]
    module.retype_bridged_images_for_trt(missing)
    onnx.checker.check_model(missing, full_check=True)
    inferred = onnx.shape_inference.infer_shapes(missing, check_type=True, strict_mode=True)
    typed = {v.name: v.type.tensor_type.elem_type for v in inferred.graph.value_info}
    assert typed["vision/img"] == TensorProto.FLOAT16
    assert typed["vision/big_img"] == TensorProto.FLOAT16

    # A conflicting explicit annotation is not silently overwritten.
    unexpected = make_graph()
    unexpected.graph.value_info[0].type.tensor_type.elem_type = TensorProto.FLOAT
    with self.assertRaisesRegex(ValueError, "Unexpected Identity value_info type"):
      module.retype_bridged_images_for_trt(unexpected)

    bad = make_graph()
    bad.graph.node.append(helper.make_node("Identity", ["vision/img"], ["extra"], name="unexpected"))
    with self.assertRaisesRegex(ValueError, "single FP16 Cast"):
      module.retype_bridged_images_for_trt(bad)

  def test_never_claims_vehicle_validation(self):
    text = SOURCE.read_text(encoding="utf-8")
    self.assertIn('"vehicle_approved": False', text)
    self.assertIn('"numerical_equivalence_verified": False', text)
    self.assertIn('"tensorrt_engine_verified": False', text)


if __name__ == "__main__":
  unittest.main()
