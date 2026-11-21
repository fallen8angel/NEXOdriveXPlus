"""CPU-only, fail-closed ONNX partition planning for the pinned Carrot N16 model.

This module never exports an engine or enables HUD/vehicle functions. A cut is
only a candidate for separately reviewed model extraction and accuracy tests.
"""
from __future__ import annotations

from math import prod

MAX_BOUNDARY_BYTES = 64 * 1024**2
MAX_BOUNDARY_TENSORS = 48
MIN_WEIGHT_FRACTION = 0.18


def _size(info):
  """Static tensor size or None; dynamic/missing types are never guessed."""
  from onnx import TensorProto
  sizes = {TensorProto.FLOAT16: 2, TensorProto.FLOAT: 4,
           TensorProto.DOUBLE: 8, TensorProto.BFLOAT16: 2,
           TensorProto.UINT8: 1, TensorProto.INT8: 1,
           TensorProto.INT32: 4, TensorProto.UINT32: 4,
           TensorProto.INT64: 8, TensorProto.UINT64: 8,
           TensorProto.BOOL: 1}
  typ = info.type
  if not typ.HasField('tensor_type'):
    return None
  tensor = typ.tensor_type
  if tensor.elem_type not in sizes or not tensor.HasField('shape'):
    return None
  dims = [d.dim_value for d in tensor.shape.dim]
  if not dims or any(not d.HasField('dim_value') or d.dim_value <= 0 for d in tensor.shape.dim):
    return None
  size = prod(dims) * sizes[tensor.elem_type]
  return {'name': info.name, 'dtype': int(tensor.elem_type),
          'shape': dims, 'nbytes': size} if size <= 8 * 1024**3 else None


def inspect_graph(model):
  """Bounded O(nodes + edges) search for a static, narrow two-stage cut."""
  from onnx import AttributeProto
  graph = model.graph
  nodes = list(graph.node)
  count = len(nodes)
  base = {'schema_version': 1, 'engine_build_attempted': False,
          'gpu_inference_verified': False, 'vehicle_control_enabled': False,
          'hud_prediction_enabled': False, 'production_engine_published': False,
          'original_node_count': count}
  def blocked(reason):
    return {**base, 'state': 'blocked', 'reason': reason, 'candidates': []}

  if count < 4:
    return blocked('Too few nodes to split')
  if any(a.type in (AttributeProto.GRAPH, AttributeProto.GRAPHS)
         for node in nodes for a in node.attribute):
    return blocked('Nested control-flow graph cannot be split automatically')
  initializers = {x.name: x for x in graph.initializer}
  inputs = {x.name for x in graph.input}
  outputs = {x.name for x in graph.output}
  available = inputs | set(initializers)
  producers = {}
  last_use = {}
  init_spans = {}
  for i, node in enumerate(nodes):
    for name in node.input:
      if not name:
        continue
      if name not in available:
        return blocked('Input is undeclared or graph nodes are not topologically ordered')
      last_use[name] = i
      if name in initializers:
        if name not in init_spans:
          init_spans[name] = [i, i]
        init_spans[name][1] = i
    for name in node.output:
      if name:
        if name in available:
          return blocked('Duplicate output tensor or input shadowing')
        producers[name] = i
        available.add(name)
  if any(out not in producers for out in outputs):
    return blocked('Output requires explicit original-input/initializer handling')

  shapes = {}
  for item in [*graph.input, *graph.value_info, *graph.output]:
    value = _size(item)
    if value:
      shapes[item.name] = value

  # Exclusive cut after node cut-1; diff arrays avoid N^2 scans.
  live_diff = [0] * (count+1)
  bytes_diff = [0] * (count+1)
  missing_diff = [0] * (count+1)
  for name, first in producers.items():
    last = last_use.get(name, first)
    if last <= first:
      continue
    live_diff[first+1] += 1
    live_diff[last+1] -= 1
    if name in shapes:
      bytes_diff[first+1] += shapes[name]['nbytes']
      bytes_diff[last+1] -= shapes[name]['nbytes']
    else:
      missing_diff[first+1] += 1
      missing_diff[last+1] -= 1

  left_diff = [0] * (count+1)
  right_diff = [0] * (count+1)
  total = 0
  for name, (first, last) in init_spans.items():
    tensor = initializers[name]
    size = len(tensor.raw_data) if tensor.raw_data else tensor.ByteSize()
    total += size
    left_diff[first+1] += size
    right_diff[last+1] -= size
  if total == 0:
    return blocked('No initializer weights for a balanced graph cut')

  live = bytes_count = missing = left = 0
  right = total
  cuts = []
  for cut in range(1, count):
    live += live_diff[cut]
    bytes_count += bytes_diff[cut]
    missing += missing_diff[cut]
    left += left_diff[cut]
    right += right_diff[cut]
    if (min(left, right) < MIN_WEIGHT_FRACTION*total or not live
        or missing or live > MAX_BOUNDARY_TENSORS
        or bytes_count > MAX_BOUNDARY_BYTES):
      continue
    shared = max(0, left+right-total)
    score = (abs(left-right)/total + 0.012*live
             + 0.2*bytes_count/MAX_BOUNDARY_BYTES + 0.2*shared/total)
    cuts.append((score, cut, left, right, bytes_count))
  cuts.sort()
  candidates = []
  for _, cut, left, right, bytes_count in cuts[:6]:
    boundary = sorted(name for name, first in producers.items()
                      if first < cut <= last_use.get(name, first))
    early = sorted(name for name in outputs if producers[name] < cut)
    late = sorted(outputs - set(early))
    if not late:
      continue
    before_inputs = sorted({x for node in nodes[:cut] for x in node.input if x in inputs})
    after_inputs = sorted({x for node in nodes[cut:] for x in node.input if x in inputs})
    candidates.append({
      'cut_node_index': cut,
      'left_weight_bytes_estimate': left,
      'right_weight_bytes_estimate': right,
      'duplicated_weight_bytes_estimate': max(0, left+right-total),
      'boundary_bytes_estimate': bytes_count,
      'boundary_tensors': [shapes[x] for x in boundary],
      'left_inputs': before_inputs,
      'left_outputs': boundary + early,
      'right_inputs': boundary + after_inputs,
      'right_outputs': late,
    })
    if len(candidates) == 3:
      break
  if not candidates:
    return {**blocked('No safe static bounded graph cut found'),
            'total_weight_bytes': total}
  return {**base, 'state': 'candidate_found',
          'total_weight_bytes': total, 'candidates': candidates,
          'note': 'No engine, GPU inference, numerical parity or HUD rendering verified'}


def inspect_pinned_model(source, release):
  """Called only after the trial runner validated exact model SHA and Offroad."""
  import sys
  import onnx
  from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images
  sys.path.insert(0, str(release / 'carrot-server/third_party/jetlink'))
  from jetlink.onnx_patch import strip_tinygrad_ops

  if source.is_symlink() or not source.is_file():
    raise ValueError('Refusing nonregular N16 ONNX')
  model = onnx.load(str(source))
  removed = strip_tinygrad_ops(model)
  patch_stateful_images(model)
  onnx.checker.check_model(model, full_check=False)
  result = inspect_graph(model)
  result.update(tinygrad_layout_nodes_removed=removed,
                original_model_file_bytes=source.stat().st_size)
  return result
