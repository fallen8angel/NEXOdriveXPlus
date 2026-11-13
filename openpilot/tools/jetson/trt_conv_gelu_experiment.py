"""Offline, opt-in TensorRT Conv/GELU compatibility experiment.

Never replaces the signed NEXO model, engine cache, or vehicle inference.
The TensorRT 10.3 fused depthwise Conv/GELU error is not yet understood.
This produces a *candidate* ONNX with a local FP32 GELU island only for
independent compilation and numerical comparison.
"""
import argparse
import hashlib
import json
from pathlib import Path

MODEL_SHA = "f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5"
CONV = "vision/node_conv2d_1"
GELU = "vision/node_gelu_1"


def make_candidate(source, destination):
  import onnx
  from onnx import TensorProto, helper

  source = Path(source).resolve(strict=True)
  destination = Path(destination).resolve()
  if source == destination:
    raise ValueError("Refusing to overwrite the original model")
  if hashlib.sha256(source.read_bytes()).hexdigest() != MODEL_SHA:
    raise ValueError("Only the exact signed NEXO ONNX model may be examined")
  if destination.exists() or destination.is_symlink():
    raise FileExistsError("Candidate destination must not exist")

  model = onnx.load(str(source))
  onnx.checker.check_model(model, full_check=True)
  nodes = {node.name: node for node in model.graph.node}
  if CONV not in nodes or GELU not in nodes:
    raise ValueError("Expected Conv/GELU node pair missing")
  conv, gelu = nodes[CONV], nodes[GELU]
  if conv.op_type != "Conv" or gelu.op_type != "Gelu":
    raise ValueError("Unexpected Conv/GELU operator types")
  if len(conv.output) != 1 or len(gelu.input) != 1 or len(gelu.output) != 1:
    raise ValueError("Unexpected Conv/GELU arity")
  boundary = conv.output[0]
  if gelu.input[0] != boundary:
    raise ValueError("Conv is not directly followed by Gelu")
  consumers = [n.name for n in model.graph.node if boundary in n.input]
  if consumers != [GELU]:
    raise ValueError("Conv output has additional consumers")
  if any(value.name == boundary for value in model.graph.output):
    raise ValueError("Conv boundary is a graph output")
  original_output = gelu.output[0]
  if not original_output or not boundary:
    raise ValueError("Empty tensor name")

  inferred = onnx.shape_inference.infer_shapes(model, check_type=True, strict_mode=True)
  tensors = {v.name: v.type.tensor_type for v in
             (*inferred.graph.input, *inferred.graph.value_info, *inferred.graph.output)}
  for name in (boundary, original_output):
    if name not in tensors or tensors[name].elem_type != TensorProto.FLOAT16:
      raise ValueError("Expected FP16 boundary and GELU output")

  # Force the GELU input/output through FP32 with explicit Cast operators.
  # The original graph remains immutable. This MAY prevent a fused FP16
  # kernel selection failure, but changes rounding and is NOT a vehicle fix.
  names = {v for node in model.graph.node for v in (*node.input, *node.output)}
  names.update(v.name for v in (*model.graph.input, *model.graph.output, *model.graph.value_info))
  fp32_input = boundary + "__nexo_diag_fp32"
  fp32_output = original_output + "__nexo_diag_fp32"
  if any(name in names for name in (fp32_input, fp32_output)):
    raise ValueError("Diagnostic tensor names already exist")
  idx = next(i for i, node in enumerate(model.graph.node) if node.name == GELU)
  cast_in = helper.make_node("Cast", [boundary], [fp32_input],
                             name="nexo_diag_cast_gelu_input_fp32", to=TensorProto.FLOAT)
  cast_out = helper.make_node("Cast", [fp32_output], [original_output],
                              name="nexo_diag_cast_gelu_output_fp16", to=TensorProto.FLOAT16)
  gelu.input[0] = fp32_input
  gelu.output[0] = fp32_output
  model.graph.node.insert(idx, cast_in)
  model.graph.node.insert(idx + 2, cast_out)
  onnx.checker.check_model(model, full_check=True)
  onnx.shape_inference.infer_shapes(model, check_type=True, strict_mode=True)
  destination.parent.mkdir(parents=True, exist_ok=True)
  onnx.save(model, str(destination))
  return {"source_sha256": MODEL_SHA,
          "candidate_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
          "candidate": str(destination),
          "experiment": "FP32 GELU island, FP16 interface",
          "vehicle_approved": False,
          "numerical_equivalence_verified": False,
          "tensorrt_engine_verified": False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--source", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True,
                      help="New offline candidate path; never use runtime cache or original ONNX")
  args = parser.parse_args()
  destination = args.output.resolve()
  protected = Path("/opt/carrot-jetlink")
  if destination == protected or protected in destination.parents:
    parser.error("Refusing to write under the live Jetson runtime")
  print(json.dumps(make_candidate(args.source, destination), indent=2))


if __name__ == "__main__":
  main()
