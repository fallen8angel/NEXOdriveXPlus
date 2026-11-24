"""Split the CPU provider's resolved whole-model graph at the original N16 producer ports."""
import hashlib
from pathlib import Path
import shutil

from openpilot.tools.jetson.nexo_n16_cpu_precision import FRAMES


def initializer_signatures(model, directory=None, names=None):
  """Compare types, dimensions and exact payloads across protobuf/external encodings."""
  from onnx import numpy_helper
  signatures = {}
  for tensor in model.graph.initializer:
    if names is not None and tensor.name not in names:
      continue
    # numpy_helper may materialize external data in its TensorProto. Work on
    # one copy so the compact execution graph never accumulates weight buffers.
    if tensor.external_data:
      from onnx import TensorProto
      local = TensorProto()
      local.CopyFrom(tensor)
      tensor = local
    array = numpy_helper.to_array(tensor, base_dir=str(directory or ''))
    signatures[tensor.name] = [tensor.data_type, list(tensor.dims), hashlib.sha256(array).hexdigest()]
  return signatures


def source_contract(model, candidate):
  ports = {}
  boundary = {item['name']: item for item in candidate['boundary_tensors']}
  for node in model.graph.node:
    for index, name in enumerate(node.output):
      if name in boundary:
        ports[name] = [node.name, node.op_type, node.domain, index]
  if set(ports) != set(boundary):
    raise ValueError('Incomplete N16 source boundary')
  return {'candidate': candidate, 'ports': ports, 'weights': initializer_signatures(model)}


def check_plan_space(stage, source_bytes):
  from openpilot.tools.jetson.nexo_n16_trt_trial import DISK_FLOOR
  from openpilot.tools.jetson.nexo_n16_cpu_workers import MAX_JOURNAL
  if shutil.disk_usage(stage).free < DISK_FLOOR + source_bytes + MAX_JOURNAL + (32 << 20):
    raise RuntimeError('Insufficient storage reserve for private CPU plan and reference journal')


def split_plan(stage: Path, contract):
  import onnx
  from onnx import TensorProto
  compact = stage / 'execution.onnx'
  if compact.is_symlink() or not 0 < compact.stat().st_size <= 24 << 20:
    raise ValueError('Unbounded CPU execution graph')
  model = onnx.load(compact, load_external_data=False)
  # The serializer may externalize large tensors only into our one private
  # backing file. Both halves will open it relative to their ONNX files.
  for initializer in model.graph.initializer:
    if initializer.external_data:
      metadata = {item.key: item.value for item in initializer.external_data}
      if metadata.get('location') != 'weights.bin' or (stage / 'weights.bin').is_symlink():
        raise ValueError('Unexpected CPU execution plan initializer location')
  weights = contract['weights']
  if initializer_signatures(model, stage, weights) != weights:
    raise ValueError('Resolved CPU plan original initializer mismatch')
  model = onnx.shape_inference.infer_shapes(model)
  graph = model.graph
  candidates = contract['candidate']
  nodes = {n.name: n for n in graph.node if n.name}
  if len(nodes) != len([n for n in graph.node if n.name]):
    raise ValueError('Ambiguous CPU plan node names')
  info = {v.name: v for v in [*graph.input, *graph.output, *graph.value_info]}
  names, edges = {}, []
  for item in candidates['boundary_tensors']:
    name = item['name']
    producer, op, domain, port = contract['ports'][name]
    node = nodes.get(producer)
    if node is None or (node.op_type, node.domain) != (op, domain) or port >= len(node.output):
      raise ValueError('Cannot identify CPU boundary producer: ' + producer)
    resolved = node.output[port]
    if resolved not in info:
      raise ValueError('Missing CPU type information for boundary: ' + name)
    vi = info[resolved].type.tensor_type
    if (vi.elem_type not in (TensorProto.FLOAT16, TensorProto.FLOAT)
        or [d.dim_value for d in vi.shape.dim] != item['shape']):
      raise ValueError('Unresolved CPU boundary shape or precision: ' + name)
    names[name] = resolved
    edges.append({'name': name, 'producer': producer, 'tensor': resolved,
                  'dtype': TensorProto.DataType.Name(vi.elem_type), 'shape': item['shape']})
  if len(set(names.values())) != len(names):
    raise ValueError('Aliased CPU boundary ports')

  extract = onnx.utils.Extractor(model)
  receipt = {'mode': 'resolved_whole_cpu_graph', 'boundary': edges, 'source_initializers_verified': len(weights)}
  for side in ('left', 'right'):
    inputs = [names.get(n, n) for n in candidates[side + '_inputs']]
    outputs = [names.get(n, n) for n in candidates[side + '_outputs']]
    part = extract.extract_model(inputs, outputs)
    used = {n for node in part.graph.node for n in node.input if n}
    supplied = set(inputs) | {v.name for v in part.graph.initializer} | {n for node in part.graph.node for n in node.output}
    if used - supplied or (side == 'right' and set(names.values()) - used):
      raise ValueError('Incomplete CPU execution graph cut: ' + side)
    path = stage / (side + '.onnx')
    onnx.save_model(part, path)
    receipt[side + '_nodes'] = len(part.graph.node)
  for edge in edges:
    graph.output.append(info[edge['tensor']])
  onnx.save_model(model, stage / 'probe.onnx')
  return receipt


def probe_first_frame(stage, spec, *, reference_frames=FRAMES):
  from openpilot.tools.jetson.nexo_n16_cpu_precision import cpu_session
  from openpilot.tools.jetson.nexo_n16_cpu_workers import ReferenceReplay, input_digest
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import compare_array, save_arrays, synthetic_feeds
  proof = split_plan(stage, read_report(stage / 'contract.json', limit=512 << 10))
  probe = cpu_session(stage / 'probe.onnx')
  feeds = synthetic_feeds(probe.get_inputs(), 0)
  observed = dict(zip([v.name for v in probe.get_outputs()], probe.run(None, feeds), strict=True))
  replay = ReferenceReplay(stage / 'reference.xz', spec, frames=reference_frames)
  try:
    expected = replay.run(None, feeds)
    # An extra graph output can affect precision. Trust this probe only if its
    # public results exactly reproduce the untouched reference on this frame.
    for descriptor, value in zip(replay.get_outputs(), expected, strict=True):
      if compare_array(value, observed[descriptor.name], descriptor.name)['max_abs_error'] != 0:
        raise ValueError('First-frame probe changed raw reference output: ' + descriptor.name)
  finally:
    replay.close()
  save_arrays(stage / 'first-boundary.npz', {e['tensor']: observed[e['tensor']] for e in proof['boundary']})
  return {**proof, 'first_frame_input_sha256': input_digest(feeds).hex(), 'probe_public_outputs_exact': True}
