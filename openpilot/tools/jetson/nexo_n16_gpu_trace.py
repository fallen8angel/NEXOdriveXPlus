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



def read_fp16_initializer(value, *, external_dir=None):
  """Read only a small Conv constant from ORT's private staged weights.bin.

  Never load all external weights or mutate the source TensorProto.
  """
  import os
  from pathlib import Path
  import stat
  import numpy as np
  from onnx import TensorProto, numpy_helper

  shape = list(value.dims)
  count = math.prod(shape)
  if (value.data_type != TensorProto.FLOAT16 or not shape or any(d <= 0 for d in shape)
      or count * 4 > 16 << 20):
    raise ValueError('Invalid or excessive FP16 Conv2d initializer')
  if value.data_location == TensorProto.EXTERNAL:
    fields = {item.key: item.value for item in value.external_data}
    if (external_dir is None or len(fields) != len(value.external_data)
        or fields.get('location') != 'weights.bin' or value.raw_data):
      raise ValueError('Untrusted external Conv2d initializer location')
    offset_text = fields.get('offset', '0')
    length_text = fields.get('length', str(count * 2))
    if not offset_text.isascii() or not offset_text.isdecimal() or not length_text.isascii() or not length_text.isdecimal():
      raise ValueError('Invalid external Conv2d initializer offset/length')
    offset, length = int(offset_text), int(length_text)
    if length != count * 2:
      raise ValueError('External Conv2d initializer length mismatch')
    path = Path(external_dir) / 'weights.bin'
    # The private stage contains the ORT-produced file. Do not follow symlinks
    # or block on non-regular files, and check bounds before reading any bytes.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
      info = os.fstat(stream.fileno())
      if not stat.S_ISREG(info.st_mode) or offset + length > info.st_size:
        raise ValueError('External Conv2d initializer exceeds staged file bounds')
      stream.seek(offset)
      raw = stream.read(length)
    if len(raw) != length:
      raise ValueError('Truncated external Conv2d initializer')
    array = np.frombuffer(raw, dtype='<f2').reshape(shape).astype(np.float16, copy=False)
  else:
    if value.external_data:
      raise ValueError('Unexpected external Conv2d initializer metadata')
    array = numpy_helper.to_array(value)
  if array.dtype != np.float16 or list(array.shape) != shape or not np.isfinite(array).all():
    raise ValueError('Invalid FP16 Conv2d initializer')
  return array


def add_fp32_conv_reference(model, producer_name='node_conv2d', *, external_dir=None):
  """Append an isolated FP32 Conv branch; never rewrite N16's original path/weights."""
  import numpy as np
  from onnx import TensorProto, helper, numpy_helper

  graph = model.graph
  matches = [node for node in graph.node
             if node.name == producer_name and node.op_type == 'Conv' and not node.domain]
  if len(matches) != 1:
    raise ValueError('Cannot uniquely identify N16 first Conv2d')
  original = matches[0]
  if len(original.input) not in (2, 3) or len(original.output) != 1:
    raise ValueError('Unexpected N16 Conv2d input/output contract')

  initializers = {value.name: value for value in graph.initializer}
  infos = {value.name: value for value in
           [*graph.input, *graph.value_info, *graph.output]}

  def static_shape(value, label):
    tensor = value.type.tensor_type
    if not tensor.HasField('shape') or any(
        not dim.HasField('dim_value') or dim.dim_value <= 0 for dim in tensor.shape.dim):
      raise ValueError('N16 Conv2d requires a static ' + label + ' shape')
    return [dim.dim_value for dim in tensor.shape.dim]

  input_info = infos.get(original.input[0])
  output_info = infos.get(original.output[0])
  weight = initializers.get(original.input[1])
  bias = initializers.get(original.input[2]) if len(original.input) == 3 and original.input[2] else None
  if (input_info is None or output_info is None or weight is None
      or (len(original.input) == 3 and original.input[2] and bias is None)):
    raise ValueError('N16 Conv2d input/output/weight metadata is incomplete')
  if input_info.type.tensor_type.elem_type != TensorProto.FLOAT16:
    raise ValueError('N16 Conv2d input is not the expected FP16 type')
  if output_info.type.tensor_type.elem_type != TensorProto.FLOAT16:
    raise ValueError('N16 Conv2d output is not the expected FP16 type')
  if weight.data_type != TensorProto.FLOAT16 or (bias is not None and bias.data_type != TensorProto.FLOAT16):
    raise ValueError('N16 Conv2d weights/bias are not the expected FP16 type')

  input_shape = static_shape(input_info, 'input')
  output_shape = static_shape(output_info, 'output')
  weight_shape = list(weight.dims)
  bias_shape = list(bias.dims) if bias is not None else None
  if (len(input_shape) != 4 or len(output_shape) != 4 or len(weight_shape) != 4
      or any(dim <= 0 for dim in weight_shape)):
    raise ValueError('N16 Conv2d weight shape is invalid')
  if bias is not None and (len(bias_shape) != 1 or bias_shape[0] != weight_shape[0]):
    raise ValueError('N16 Conv2d bias shape is invalid')
  if math.prod(output_shape) * 4 > 16 << 20:
    raise ValueError('FP32 Conv2d reference output exceeds the 16 MiB bound')

  output_name = 'nexo_n16_conv2d_fp32_reference'
  cast_input = output_name + '_input'
  cast_weight = output_name + '_weight'
  cast_bias = output_name + '_bias'
  generated = {output_name, cast_input, cast_weight}
  if bias is not None:
    generated.add(cast_bias)
  occupied = ({value.name for value in graph.input} | {value.name for value in graph.output}
              | {value.name for value in graph.value_info} | set(initializers)
              | {name for node in graph.node for name in (*node.input, *node.output) if name})
  if generated & occupied:
    raise ValueError('FP32 Conv2d reference tensor name collision')

  # TensorRT 10.3 can route a runtime Cast of a Conv kernel through its
  # convMultiInput importer instead of the static-weight importer. Materialize
  # exact FP32 copies in this scratch graph; leave the original constants alone.
  def fp32_constant(value, name):
    array = read_fp16_initializer(value, external_dir=external_dir)
    return numpy_helper.from_array(array.astype(np.float32), name=name)

  nodes = [helper.make_node('Cast', [original.input[0]], [cast_input],
                            name=output_name + '_cast_input', to=TensorProto.FLOAT)]
  constants = [fp32_constant(weight, cast_weight)]
  conv_inputs = [cast_input, cast_weight]
  value_info = [helper.make_tensor_value_info(cast_input, TensorProto.FLOAT, input_shape)]
  if bias is not None:
    constants.append(fp32_constant(bias, cast_bias))
    conv_inputs.append(cast_bias)

  attributes = {}
  for attribute in original.attribute:
    value = helper.get_attribute_value(attribute)
    if isinstance(value, bytes):
      value = value.decode('utf-8', errors='replace')
    elif isinstance(value, tuple):
      value = list(value)
    elif hasattr(value, 'tolist'):
      value = value.tolist()
    attributes[attribute.name] = value
  group = attributes.get('group', 1)
  if (type(group) is not int or group < 1 or input_shape[1] != weight_shape[1] * group
      or weight_shape[0] % group or output_shape[1] != weight_shape[0]
      or input_shape[0] != output_shape[0]):
    raise ValueError('FP32 Conv2d channel/group mismatch')
  if list(attributes.get('kernel_shape', weight_shape[2:])) != weight_shape[2:]:
    raise ValueError('FP32 Conv2d kernel shape mismatch')
  attributes['kernel_shape'] = weight_shape[2:]
  nodes.append(helper.make_node('Conv', conv_inputs, [output_name],
                                name=output_name, **attributes))
  graph.initializer.extend(constants)
  graph.node.extend(nodes)
  graph.value_info.extend(value_info)
  graph.output.append(helper.make_tensor_value_info(output_name, TensorProto.FLOAT, output_shape))
  return {
    'output_name': output_name,
    'original_output_name': original.output[0],
    'input_name': original.input[0],
    'weight_name': original.input[1],
    'bias_name': original.input[2] if bias is not None else None,
    'reference_precision': 'FP32 input/weights/bias/output',
    'reference_constants': 'static_exact_fp32_copies',
    'source_external_data': weight.data_location == TensorProto.EXTERNAL or (bias is not None and bias.data_location == TensorProto.EXTERNAL),
    'source_precision': 'original FP16 input/weights/bias/output',
    'weights_modified': False,
    'original_conv_modified': False,
    'root_cause_confirmed': False,
  }



def instrument(model, targets):
  """Expose each selected producer's runtime operands and result in a scratch graph."""
  from onnx import helper
  graph = model.graph
  info = {v.name: v for v in [*graph.input, *graph.value_info, *graph.output]}
  producers = {name: node for node in graph.node for name in node.output if name}
  if tuple(targets) == ('conv-isolated',):
    matches = [node for node in graph.node if node.name == 'node_conv2d' and node.op_type == 'Conv' and not node.domain]
    if len(matches) != 1 or not matches[0].output[0]:
      raise ValueError('Cannot identify N16 isolated Conv checkpoint')
    targets = [matches[0].output[0]]
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


def trace_cpu(stage, targets, patched, *, original_ops_only=False):
  import onnx
  from openpilot.tools.jetson.nexo_n16_cpu_precision import cpu_session
  from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images
  model = onnx.load(stage / 'left.onnx', load_external_data=False)
  trace, names = instrument(model, targets)
  fp32_reference = None
  if patched:
    patch_stateful_images(model)
    if not original_ops_only and any(value.get('producer') == 'node_conv2d' for value in trace.values()):
      fp32_reference = add_fp32_conv_reference(model, external_dir=stage)
      info = {value.name: value for value in
              [*model.graph.input, *model.graph.value_info, *model.graph.output]}
      if len(names) >= 16:
        raise ValueError('FP32 Conv2d reference exceeds the trace tensor-count bound')
      current_bytes = 0
      from onnx import helper
      for name in names:
        tensor = info[name].type.tensor_type
        shape = [dim.dim_value for dim in tensor.shape.dim]
        current_bytes += math.prod(shape) * helper.tensor_dtype_to_np_dtype(tensor.elem_type).itemsize
      reference_shape = [dim.dim_value for dim in
                         info[fp32_reference['output_name']].type.tensor_type.shape.dim]
      reference_bytes = math.prod(reference_shape) * 4
      if current_bytes + reference_bytes > 32 << 20:
        raise ValueError('FP32 Conv2d reference exceeds the total trace memory bound')
      names.append(fp32_reference['output_name'])
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
  if fp32_reference:
    sampled.discard(fp32_reference['output_name'])
  order = [{'tensor': name, 'producer': node.name, 'op': node.op_type, 'resolved_node_index': index}
           for index, node in enumerate(model.graph.node) for name in node.output if name in sampled]
  conv_evidence = conv_precision_evidence(model) if any(v.get('producer') == 'node_conv2d' for v in trace.values()) else None
  return {'producers': trace, 'sampled_order': order, 'names': names, 'boundary_errors': boundary_errors,
          'conv_precision_evidence': conv_evidence, 'fp32_conv_reference': fp32_reference}


def replay_trace(stage, *, baseline, build_role=None):
  from jetlink.server.backends.trt.engine import TrtEngine
  from openpilot.tools.jetson.nexo_n16_cpu_precision import image_adapter
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  from openpilot.tools.jetson.nexo_n16_split_gpu import GpuSession
  from openpilot.tools.jetson.nexo_n16_trt_trial import digest
  stem = 'left' if baseline else 'left-trace'
  plan = stage / (stem + '.plan')
  receipt_role = build_role or ('build_' + stem.replace('-', '_'))
  receipt = read_report(stage / (receipt_role + '-result.json'))
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
    if build_role is None:
      save_arrays(stage / 'plain-gpu.npz', outputs)
    return {'boundary_errors': {n: compare_array(v, outputs[n], n) for n, v in boundary.items()}}

  original = load_arrays(stage / 'trace-original.npz')
  patched = load_arrays(stage / 'trace-patched.npz')
  plain = load_arrays(stage / 'plain-gpu.npz')
  tap_errors = {n: compare_array(plain[n], outputs[n], n)['max_abs_error'] for n in boundary}
  patch_errors, gpu_errors = {}, {}
  fp32_reference = meta.get('fp32_conv_reference')
  fp32_reference_name = fp32_reference.get('output_name') if fp32_reference else None
  for name in meta['names']:
    if name in original:
      base = original[name]
      if base.dtype != patched[name].dtype:
        base = image_adapter(base, patched[name].dtype)
      patch_errors[name] = compare_array(base, patched[name], name)
    elif name != fp32_reference_name:
      raise ValueError('Unexpected tensor missing from original CPU trace: ' + name)
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
  precision_diagnosis = None
  if fp32_reference_name is not None:
    import numpy as np
    conv_points = [point for point in meta['sampled_order'] if point.get('producer') == 'node_conv2d']
    if len(conv_points) != 1:
      raise ValueError('FP32 Conv2d reference has no unique original Conv output')
    conv_output = conv_points[0]['tensor']
    def compare_mixed(expected, actual, name):
      return compare_array(np.asarray(expected, dtype=np.float32),
                           np.asarray(actual, dtype=np.float32), name)
    cpu_fp16, cpu_fp32 = patched[conv_output], patched[fp32_reference_name]
    gpu_fp16, gpu_fp32 = outputs[conv_output], outputs[fp32_reference_name]
    precision_diagnosis = {
      'original_conv_output': conv_output,
      'fp32_reference_output': fp32_reference_name,
      'cpu_fp16_vs_cpu_fp32': compare_mixed(cpu_fp32, cpu_fp16, 'cpu_fp16_vs_cpu_fp32'),
      'gpu_fp16_vs_cpu_fp16': gpu_errors[conv_output],
      'gpu_fp32_vs_cpu_fp32': gpu_errors[fp32_reference_name],
      'gpu_fp16_vs_cpu_fp32': compare_mixed(cpu_fp32, gpu_fp16, 'gpu_fp16_vs_cpu_fp32'),
      'gpu_fp16_vs_gpu_fp32': compare_mixed(gpu_fp32, gpu_fp16, 'gpu_fp16_vs_gpu_fp32'),
      'precision_root_cause_confirmed': False,
      'production_activation_allowed': False,
    }
    if (anchor_changed or patch_changed
        or not gpu_errors.get(fp32_reference['input_name'], {}).get('passed', False)):
      precision_diagnosis['interpretation'] = 'Inconclusive: the original graph anchor, CPU patch, or Conv input differs or was not sampled.'
    elif precision_diagnosis['gpu_fp16_vs_cpu_fp16']['passed'] and precision_diagnosis['gpu_fp32_vs_cpu_fp32']['passed']:
      precision_diagnosis['interpretation'] = 'TensorRT matches the CPU reference for both original FP16 and isolated FP32 Conv paths; FP16-versus-FP32 delta alone does not establish a GPU defect.'
    elif (not precision_diagnosis['gpu_fp16_vs_cpu_fp16']['passed']
          and precision_diagnosis['gpu_fp32_vs_cpu_fp32']['passed']):
      precision_diagnosis['interpretation'] = 'FP32 Conv parity passes while original FP16 Conv parity fails; the FP16 execution path is implicated but root cause is not confirmed.'
    elif not precision_diagnosis['gpu_fp32_vs_cpu_fp32']['passed']:
      precision_diagnosis['interpretation'] = 'The isolated FP32 Conv path also diverges; investigate graph semantics, builder tactics and runtime before attributing the issue to FP16.'
    else:
      precision_diagnosis['interpretation'] = 'The isolated comparison is inconclusive; inspect operand and boundary evidence before changing precision.'
  return {'earliest_sampled_mismatch': earliest, 'sampled_order': meta['sampled_order'],
          'state': 'gpu_left_trace_complete', 'phase': 'offline_gpu_left_trace_complete',
          'trace_frame': 0, 'policy_scope': 'instrumented_left_engine_only', 'producers': observations,
          'cpu_image_patch_errors': patch_errors, 'patched_cpu_to_gpu_errors': gpu_errors,
          'patched_cpu_to_reference_boundary_errors': meta['boundary_errors'],
          'plain_to_instrumented_gpu_boundary_errors': tap_errors,
          'conv_precision_evidence': meta.get('conv_precision_evidence'),
          'fp32_conv_reference': fp32_reference,
          'conv_precision_diagnosis': precision_diagnosis,
          'gpu_parity_verified': False, 'production_activation_allowed': False}
