"""Checked NEXO image-input adaptation for the pinned Carrot TensorRT builder.

Only UINT8 -> FLOAT16 head casts may be replaced. Identity bridges and a
pre-cast Concat preserve values; every consumer before the casts is checked.
The shipped ONNX and its model/cache identity are never changed.
"""
import copy
import hashlib
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper
from onnx.reference import ReferenceEvaluator

IMAGES = ('img', 'big_img')
PINNED_PATCH_SHA256 = '2551902a7dd4fb0a50d046696f17bdb073c02ba3c4dddba6e63cf8b89aedc503'


def head_plan(model):
  """Return the complete UINT8 region and FLOAT16 frontier, or fail closed."""
  graph = model.graph
  inputs = {vi.name: vi for vi in graph.input if vi.name in IMAGES}
  if set(inputs) != set(IMAGES) or any(vi.type.tensor_type.elem_type != TensorProto.UINT8 for vi in inputs.values()):
    raise ValueError('Both img and big_img must be UINT8 inputs before adaptation')
  raw, region, casts = set(IMAGES), [], []
  for index, node in enumerate(graph.node):
    if any(a.type in (onnx.AttributeProto.GRAPH, onnx.AttributeProto.GRAPHS) for a in node.attribute):
      raise ValueError('Nested graph image capture is not supported by the input preflight')
    if not raw.intersection(node.input):
      continue
    if node.domain not in ('', 'ai.onnx') or len(node.output) != 1 or not node.output[0]:
      raise ValueError(f'Unsupported image head node: {node.name}')
    if node.op_type == 'Cast':
      if (len(node.input) != 1 or len(node.attribute) != 1 or node.attribute[0].name != 'to'
          or helper.get_attribute_value(node.attribute[0]) != TensorProto.FLOAT16):
        raise ValueError(f'Image head Cast must target FLOAT16: {node.name}')
      casts.append(index)
    elif node.op_type == 'Identity' and len(node.input) == 1 and not node.attribute:
      raw.add(node.output[0])
    elif (node.op_type == 'Concat' and len(node.input) >= 2 and set(node.input) <= raw
          and len(node.attribute) == 1 and node.attribute[0].name == 'axis'):
      raw.add(node.output[0])
    else:
      raise ValueError(f'Unsafe UINT8 image consumer before FLOAT16 Cast: {node.name} ({node.op_type})')
    region.append(index)
  if not casts or any(vi.name in raw for vi in graph.output):
    raise ValueError('Image paths must end in FLOAT16 Casts without exposing UINT8 graph outputs')
  consumed = {name for index in region for name in graph.node[index].input}
  if raw - consumed:
    raise ValueError('An image path does not terminate at a FLOAT16 Cast')
  for vi in (*graph.input, *graph.value_info):
    if vi.name in raw and vi.type.tensor_type.elem_type != TensorProto.UINT8:
      raise ValueError(f'Inconsistent UINT8 image value_info: {vi.name}')
  return raw, region, casts


def _prefix(model, region, casts):
  graph = helper.make_graph(
    [model.graph.node[i] for i in region], 'checked_image_prefix',
    [vi for vi in model.graph.input if vi.name in IMAGES],
    [helper.make_tensor_value_info(model.graph.node[i].output[0], TensorProto.FLOAT16, None) for i in casts])
  return helper.make_model(graph, opset_imports=model.opset_import, ir_version=model.ir_version)


def verify_image_values(original, adapted, region, casts):
  """Execute the actual head paths for all 256 byte values, comparing FP16 bits."""
  feeds = {}
  for vi in original.graph.input:
    if vi.name not in IMAGES:
      continue
    shape = tuple(d.dim_value for d in vi.type.tensor_type.shape.dim)
    if not shape or any(d <= 0 for d in shape):
      raise ValueError('Image preflight requires static positive input shapes')
    size = int(np.prod(shape))
    if size > 16 * 1024 * 1024:
      raise ValueError('Image preflight exceeds its input allocation budget')
    # Different image streams also exercise Concat order. Multiple passes cover
    # all byte values for tiny synthetic models as well as full-size NEXO.
    feeds[vi.name] = (np.arange(size, dtype=np.uint32) + (73 if vi.name == 'big_img' else 0)).reshape(shape)
  before = ReferenceEvaluator(_prefix(original, region, casts))
  after = ReferenceEvaluator(_prefix(adapted, region, casts))
  smallest = min(value.size for value in feeds.values())
  for offset in range(0, 256, min(smallest, 256)):
    raw = {name: ((value + offset) % 256).astype(np.uint8) for name, value in feeds.items()}
    expected = before.run(None, raw)
    actual = after.run(None, {name: value.astype(np.float16) for name, value in raw.items()})
    for lhs, rhs in zip(expected, actual, strict=True):
      if (lhs.dtype != np.float16 or rhs.dtype != np.float16 or lhs.shape != rhs.shape
          or not np.array_equal(lhs.view(np.uint16), rhs.view(np.uint16))):
        raise ValueError('UINT8/FLOAT16 image head numerical parity failed')


def adapt(model):
  """Return a checked copy. A failed conversion leaves the caller's graph intact."""
  onnx.checker.check_model(model, full_check=True)
  raw, region, casts = head_plan(model)
  result = copy.deepcopy(model)
  for vi in (*result.graph.input, *result.graph.value_info):
    if vi.name in raw:
      vi.type.tensor_type.elem_type = TensorProto.FLOAT16
  # Keep Cast output names, graph outputs and value_info alive. After retyping
  # the entire UINT8 region, these exact head casts are now FP16 identities.
  for index in casts:
    node = result.graph.node[index]
    node.op_type = 'Identity'
    del node.attribute[:]
  onnx.checker.check_model(result, full_check=True)
  verify_image_values(model, result, region, casts)
  return result


def _vendor_patch():
  from jetlink import onnx_patch
  source = Path(onnx_patch.__file__).read_bytes().replace(b'\r\n', b'\n')
  if hashlib.sha256(source).hexdigest() != PINNED_PATCH_SHA256:
    raise ValueError('NEXO ONNX adapter requires the pinned Carrot onnx_patch.py')
  return onnx_patch


def _prepare(path):
  model = onnx.load(str(path))
  _vendor_patch().strip_tinygrad_ops(model)
  return adapt(model)


def preflight(path, expected_sha256):
  """CPU-only graph/type/parity gate, including when a cached engine exists."""
  path = Path(path)
  if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
    raise ValueError('ONNX preflight model SHA differs from the selected NEXO model')
  adapted = _prepare(path)
  return {'model_sha256': expected_sha256, 'image_inputs': list(IMAGES),
          'image_dtype': 'FLOAT16', 'uint8_values_checked': 256, 'max_abs_error': 0,
          'output_names': [vi.name for vi in adapted.graph.output]}


def patch_file(src, dst, check=True):
  """Carrot build hook: mandatory preflight before the TensorRT parser runs."""
  if Path(src).resolve() == Path(dst).resolve():
    raise ValueError('Refusing to overwrite the shipped ONNX model')
  model = _prepare(src)  # The check flag cannot disable the NEXO validation gate.
  onnx.save(model, str(dst))
  return dst


def install_carrot_patch():
  """Process-local adapter; the pinned vendor files on DATA remain untouched."""
  _vendor_patch().patch_file = patch_file
