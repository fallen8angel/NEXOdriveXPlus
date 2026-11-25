"""One-frame, left-engine-only localization. Trace results never grant parity."""
import math

from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import POLICY, compare_array, load_arrays, save_arrays, synthetic_feeds

DEFAULT_TARGETS = ('InsertedPrecisionFreeCast_add_27', 'InsertedPrecisionFreeCast_val_471')



def conv_precision_evidence(model, producer_name='node_conv2d'):
  """Describe the pinned first Conv and constants without rewriting the model.

  Diagnostic evidence only: this does not infer FP16 as the root cause,
  alter precision, or authorize a TensorRT/vehicle engine.
  """
  import hashlib
  from onnx import TensorProto, helper

  matches = [node for node in model.graph.node
             if node.name == producer_name and node.op_type == 'Conv' and not node.domain]
  if len(matches) != 1:
    raise ValueError('Cannot uniquely identify N16 first Conv2d')
  node = matches[0]
  if len(node.input) not in (2, 3) or len(node.output) != 1:
    raise ValueError('Unexpected N16 Conv2d input/output contract')
  initializers = {item.name: item for item in model.graph.initializer}
  values = {item.name: item for item in
            [*model.graph.input, *model.graph.value_info, *model.graph.output]}

  def tensor_meta(name):
    value = values.get(name)
    if value is None:
      return {'name': name, 'metadata_available': False}
    tensor = value.type.tensor_type
    dims = [dim.dim_value if dim.HasField('dim_value') else None for dim in tensor.shape.dim]
    return {'name': name, 'dtype': TensorProto.DataType.Name(tensor.elem_type),
            'shape': dims, 'metadata_available': True}

  def constant_meta(name):
    value = initializers.get(name)
    if value is None:
      return {'name': name, 'present': False}
    payload = value.SerializeToString()
    return {'name': name, 'present': True,
            'dtype': TensorProto.DataType.Name(value.data_type),
            'shape': list(value.dims), 'serialized_bytes': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest()}

  attributes = {}
  for attr in sorted(node.attribute, key=lambda item: item.name):
    value = helper.get_attribute_value(attr)
    if isinstance(value, bytes):
      value = value.decode('utf-8', errors='replace')
    elif hasattr(value, 'tolist'):
      value = value.tolist()
    elif isinstance(value, tuple):
      value = list(value)
    attributes[attr.name] = value
  return {
    'producer': node.name, 'op': node.op_type, 'domain': node.domain,
    'input': tensor_meta(node.input[0]),
    'weight': constant_meta(node.input[1]),
    'bias': constant_meta(node.input[2]) if len(node.input) == 3 and node.input[2] else None,
    'output': tensor_meta(node.output[0]), 'attributes': attributes,
    'model_rewritten': False, 'weights_modified': False,
    'precision_root_cause_confirmed': False,
    'next_probe': 'compare isolated FP32 Conv reference against original CPU and TensorRT outputs',
    'production_activation_allowed': False,
  }


def instrument(model, targets):
  """Expose each selected producer's runtime operands and result in a scratch graph."""
  from onnx import helper
  graph = model.graph
  info = {v.name: v for v in [*graph.input, *graph.value_info, *graph.output]}
  producers = {name: node for node in graph.node for name in node.output if name}
  if tuple(targets) == ('encoder-stages',):
    # Resolve producer ports in ORT's accepted graph; ORT may rename FP32 edges.
    selected = []
    for producer, op in (('node_conv2d', 'Conv'), ('node_layer_norm', 'LayerNormalization'),
                         ('node_add_6', 'Add'), ('node_add_16', 'Add')):
      matches = [node for node in graph.node if node.name == producer and node.op_type == op and not node.domain]
      if len(matches) != 1 or not matches[0].output[0]:
        raise ValueError('Cannot identify N16 encoder checkpoint: ' + producer)
      selected.append(matches[0].output[0])
    targets = selected
  constants = {v.name for v in graph.initializer}
  inputs = {v.name for v in graph.input}
  public = {v.name for v in graph.output}
  trace = {}
  names = []
  for target in targets:
    if target not in producers:
      raise ValueError('Missing left-side trace producer: ' + target)
    node = producers[target]
    operands = [s for s in node.input if s and s not in constants]
    trace[target] = {'producer': node.name, 'op': node.op_type, 'operands': operands,
                     'graph_inputs': [s for s in operands if s in inputs]}
    names.extend(s for s in operands if s not in inputs)
    names.append(target)
  names = list(dict.fromkeys(names))
  total = 0
  for name in names:
    if name not in info:
      raise ValueError('Missing trace tensor metadata: ' + name)
    tensor = info[name].type.tensor_type
    if not tensor.HasField('shape') or any(d.dim_value <= 0 for d in tensor.shape.dim):
      raise ValueError('Unknown trace shape: ' + name)
    size = math.prod(d.dim_value for d in tensor.shape.dim) * helper.tensor_dtype_to_np_dtype(tensor.elem_type).itemsize
    total += size
    if size > 16 << 20 or total > 32 << 20 or len(names) > 16:
      raise ValueError('Selected trace exceeds memory bound: ' + name)
    if name not in public:
      graph.output.append(info[name])
      public.add(name)
  return trace, names


def trace_cpu(stage, targets, patched):
  import onnx
  from openpilot.tools.jetson.nexo_n16_cpu_precision import cpu_session
  from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images
  model = onnx.load(stage / 'left.onnx', load_external_data=False)
  trace, names = instrument(model, targets)
  if patched:
    patch_stateful_images(model)
  path = stage / ('left-trace-trt.onnx' if patched else 'left-trace.onnx')
  onnx.save_model(model, path)
  session = cpu_session(path)
  boundary = load_arrays(stage / 'first-boundary.npz')
  requested = list(dict.fromkeys([*names, *boundary]))
  # Every input (including both recurrent queues) is zero on the verified first frame.
  values = dict(zip(requested, session.run(requested, synthetic_feeds(session.get_inputs(), 0)), strict=True))
  boundary_errors = {k: compare_array(v, values[k], k)['max_abs_error'] for k, v in boundary.items()}
  if not patched and any(boundary_errors.values()):
    raise ValueError('Instrumented left CPU graph changed a verified first-frame boundary')
  save_arrays(stage / ('trace-patched.npz' if patched else 'trace-original.npz'), {n: values[n] for n in names})
  sampled = set(names)
  order = [{'tensor': name, 'producer': node.name, 'op': node.op_type, 'resolved_node_index': index}
           for index, node in enumerate(model.graph.node) for name in node.output if name in sampled]
  conv_evidence = conv_precision_evidence(model) if any(v.get('producer') == 'node_conv2d' for v in trace.values()) else None
  return {'producers': trace, 'sampled_order': order, 'names': names, 'boundary_errors': boundary_errors,
          'conv_precision_evidence': conv_evidence}


def replay_trace(stage, *, baseline):
  from jetlink.server.backends.trt.engine import TrtEngine
  from openpilot.tools.jetson.nexo_n16_cpu_precision import image_adapter
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  from openpilot.tools.jetson.nexo_n16_split_gpu import GpuSession
  from openpilot.tools.jetson.nexo_n16_trt_trial import digest
  stem = 'left' if baseline else 'left-trace'
  plan = stage / (stem + '.plan')
  receipt = read_report(stage / ('build_' + stem.replace('-', '_') + '-result.json'))
  if receipt['bytes'] != plan.stat().st_size or receipt['sha256'] != digest(plan):
    raise ValueError('Left trace plan identity changed')
  boundary = load_arrays(stage / 'first-boundary.npz')
  meta = read_report(stage / 'trace_patched-result.json')
  names = list(boundary) if baseline else list(dict.fromkeys([*meta['names'], *boundary]))
  engine = TrtEngine(str(plan))
  try:
    session = GpuSession(engine, stage / (stem + '-trt.onnx'))
    outputs = dict(zip(names, session.run(names, synthetic_feeds(session.get_inputs(), 0)), strict=True))
  finally:
    engine.close()
  if baseline:
    save_arrays(stage / 'plain-gpu.npz', outputs)
    return {'boundary_errors': {n: compare_array(v, outputs[n], n) for n, v in boundary.items()}}

  original = load_arrays(stage / 'trace-original.npz')
  patched = load_arrays(stage / 'trace-patched.npz')
  plain = load_arrays(stage / 'plain-gpu.npz')
  tap_errors = {n: compare_array(plain[n], outputs[n], n)['max_abs_error'] for n in boundary}
  patch_errors, gpu_errors = {}, {}
  for name in meta['names']:
    base = original[name]
    if base.dtype != patched[name].dtype:
      base = image_adapter(base, patched[name].dtype)
    patch_errors[name] = compare_array(base, patched[name], name)
    gpu_errors[name] = compare_array(patched[name], outputs[name], name)
  anchor_changed = any(tap_errors.values())
  patch_changed = (not all(m['passed'] for m in patch_errors.values())
                   or any(v > POLICY['atol'] for v in meta['boundary_errors'].values()))
  observations = {}
  for name, node in meta['producers'].items():
    upstream = [n for n in node['operands'] if n in gpu_errors and not gpu_errors[n]['passed']]
    if anchor_changed:
      finding = 'instrumentation_changed_boundary'
    elif patch_changed:
      finding = 'cpu_image_patch_differs'
    elif upstream:
      finding = 'trace_upstream_operands'
    elif not gpu_errors[name]['passed']:
      finding = 'output_exceeds_tolerance_with_operands_in_tolerance'
    else:
      finding = 'within_tolerance_at_checkpoint'
    observations[name] = {**node, 'finding': finding, 'divergent_operands': upstream}
  # A threshold crossing is attributable only to the sampled, CPU-anchored graph.
  # An unsampled ancestor may already diverge, even when its descendant agrees.
  earliest = None
  if not (anchor_changed or patch_changed):
    earliest = next((point for point in meta['sampled_order'] if not gpu_errors[point['tensor']]['passed']), None)
  return {'earliest_sampled_mismatch': earliest, 'sampled_order': meta['sampled_order'],
          'state': 'gpu_left_trace_complete', 'phase': 'offline_gpu_left_trace_complete',
          'trace_frame': 0, 'policy_scope': 'instrumented_left_engine_only', 'producers': observations,
          'cpu_image_patch_errors': patch_errors, 'patched_cpu_to_gpu_errors': gpu_errors,
          'patched_cpu_to_reference_boundary_errors': meta['boundary_errors'],
          'plain_to_instrumented_gpu_boundary_errors': tap_errors,
          'gpu_parity_verified': False, 'production_activation_allowed': False}
