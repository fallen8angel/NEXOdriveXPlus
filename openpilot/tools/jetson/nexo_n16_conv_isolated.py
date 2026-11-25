"""Scratch-only first Conv: independent FP64 oracle and separate typed engines."""
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import compare_array, load_arrays, save_arrays

PRESET = ('conv-isolated',)
SIDES = ('conv16', 'conv32')


def independent_conv(x, weight, bias):
  """Reference for the pinned non-overlapping NCHW stem, not a general Conv."""
  import numpy as np

  if (x.ndim != 4 or weight.ndim != 4 or x.shape[0] != 1
      or x.shape[1] != weight.shape[1] or min(*x.shape, *weight.shape) <= 0
      or bias.shape != (weight.shape[0],)):
    raise ValueError('Unsupported isolated Conv dimensions')
  n, c, height, width = x.shape
  out, _, kh, kw = weight.shape
  if height % kh or width % kw:
    raise ValueError('Isolated Conv requires complete nonoverlapping patches')
  oh, ow = height // kh, width // kw
  if (x.size * 8 > 16 << 20 or weight.size * 8 > 16 << 20
      or n * out * oh * ow * 8 > 16 << 20 or oh * ow * out * c * kh * kw > 300_000_000):
    raise ValueError('Isolated Conv exceeds bounded oracle work')
  if not all(np.isfinite(v).all() for v in (x, weight, bias)):
    raise ValueError('Nonfinite isolated Conv operand')
  patches = x.reshape(n, c, oh, kh, ow, kw).transpose(0, 2, 4, 1, 3, 5)
  patches = patches.reshape(-1, c * kh * kw)
  w = weight.reshape(out, -1).astype(np.float64)
  result = np.empty((oh * ow, out), dtype=np.float64)
  for start in range(0, len(patches), 128):
    result[start:start + 128] = patches[start:start + 128].astype(np.float64) @ w.T + bias.astype(np.float64)
  return result.reshape(n, oh, ow, out).transpose(0, 3, 1, 2).copy()


def prepare_isolated_conv(stage):
  """Use the verified CPU trace's actual input; no modification of the left graph."""
  import numpy as np
  import onnx
  from onnx import TensorProto as T, helper as h, numpy_helper
  from openpilot.tools.jetson.nexo_n16_gpu_trace import conv_precision_evidence, read_fp16_initializer
  from openpilot.tools.jetson.nexo_n16_cpu_precision import cpu_session

  model = onnx.load(stage / 'left.onnx', load_external_data=False)
  evidence = conv_precision_evidence(model)
  node = next(v for v in model.graph.node if v.name == evidence['producer'])
  attrs = evidence['attributes']
  if set(attrs) - {'auto_pad', 'dilations', 'group', 'kernel_shape', 'pads', 'strides'}:
    raise ValueError('Unexpected isolated Conv attributes')
  if any(evidence[k].get('dtype') != 'FLOAT16' for k in ('input', 'output', 'weight')):
    raise ValueError('Expected pinned FP16 Conv contract')
  constants = {v.name: v for v in model.graph.initializer}
  weight = read_fp16_initializer(constants[node.input[1]], external_dir=stage)
  bias = (read_fp16_initializer(constants[node.input[2]], external_dir=stage)
          if len(node.input) == 3 and node.input[2] else np.zeros(weight.shape[0], np.float16))
  kernel = list(weight.shape[2:])
  if (len(kernel) != 2 or attrs.get('auto_pad', 'NOTSET') != 'NOTSET'
      or attrs.get('group', 1) != 1 or list(attrs.get('pads', [0] * 4)) != [0] * 4
      or list(attrs.get('dilations', [1, 1])) != [1, 1]
      or list(attrs.get('strides', [1, 1])) != kernel
      or list(attrs.get('kernel_shape', kernel)) != kernel):
    raise ValueError('Unsupported isolated Conv geometry')
  original = load_arrays(stage / 'trace-original.npz')
  x, original_output = original[node.input[0]], original[node.output[0]]
  if (x.dtype != np.float16 or original_output.dtype != np.float16
      or list(x.shape) != evidence['input']['shape']
      or list(original_output.shape) != evidence['output']['shape']):
    raise ValueError('Isolated Conv source input/output mismatch')
  # One real intermediate from the verified synthetic frame, plus one
  # deterministic nonuniform artificial tensor. Neither uses live cameras.
  probe = ((np.arange(x.size, dtype=np.int32) % 31 - 15).reshape(x.shape) / 16).astype(np.float16)
  cases = (x, probe)
  oracle = [independent_conv(a, weight, bias) for a in cases]
  if any(v.shape != original_output.shape for v in oracle):
    raise ValueError('Isolated Conv output geometry mismatch')
  if any(np.max(np.abs(v)) > np.finfo(np.float16).max for v in oracle):
    raise ValueError('Isolated Conv FP16 reference overflow')
  archive = {}
  report = {'scope': 'first_conv_only_two_synthetic_inputs', 'policy_scope': 'diagnostic_only',
            'conv_precision_evidence': evidence, 'cases': [], 'production_activation_allowed': False}
  for index, (a, ref) in enumerate(zip(cases, oracle, strict=True)):
    archive[f'input{index}'] = a
    archive[f'oracle{index}'] = ref
    report['cases'].append({'case': index, 'input_kind': 'verified_frame_zero_conv_operand' if index == 0 else 'nonuniform_synthetic_operand'})
  for side, dtype, tensor_type in (('conv16', np.float16, T.FLOAT16), ('conv32', np.float32, T.FLOAT)):
    graph = h.make_graph([
      h.make_node('Conv', ['conv_input', 'weight', 'bias'], ['conv_output'],
                  name='isolated_first_conv', kernel_shape=kernel, strides=kernel),
    ], side, [h.make_tensor_value_info('conv_input', tensor_type, list(x.shape))],
       [h.make_tensor_value_info('conv_output', tensor_type, list(original_output.shape))],
       initializer=[numpy_helper.from_array(weight.astype(dtype), name='weight'),
                    numpy_helper.from_array(bias.astype(dtype), name='bias')])
    small = h.make_model(graph, ir_version=10, opset_imports=[h.make_opsetid('', 17)])
    onnx.checker.check_model(small)
    path = stage / (side + '.onnx')
    if path.exists() or path.is_symlink():
      raise ValueError('Refusing overwrite of isolated Conv graph')
    onnx.save_model(small, path)
    session = cpu_session(path)
    for index, a in enumerate(cases):
      out = session.run(['conv_output'], {'conv_input': a.astype(dtype)})[0]
      archive[f'{side}_cpu{index}'] = out
      report['cases'][index][side + '_cpu_vs_oracle'] = compare_array(oracle[index], out.astype(np.float64), side)
      report['cases'][index][side + '_cpu_vs_rounded_oracle'] = compare_array(oracle[index].astype(dtype), out, side)
    del session
  report['original_graph_cpu_vs_isolated_fp16_cpu'] = compare_array(original_output, archive['conv16_cpu0'], 'original_vs_isolated')
  report['original_graph_cpu_vs_fp64_oracle'] = compare_array(oracle[0], original_output.astype(np.float64), 'original_vs_oracle')
  save_arrays(stage / 'conv-isolated-inputs.npz', archive)
  return report


def replay_isolated_conv(stage, side):
  import numpy as np
  from jetlink.server.backends.trt.engine import TrtEngine
  from openpilot.tools.jetson.nexo_n16_split_gpu import GpuSession
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  from openpilot.tools.jetson.nexo_n16_trt_trial import digest

  if side not in SIDES:
    raise ValueError('Unknown isolated Conv precision')
  plan = stage / (side + '.plan')
  receipt = read_report(stage / ('build_' + side + '-result.json'))
  if receipt['bytes'] != plan.stat().st_size or receipt['sha256'] != digest(plan):
    raise ValueError('Isolated Conv plan identity changed')
  arrays = load_arrays(stage / 'conv-isolated-inputs.npz')
  dtype = np.float16 if side == 'conv16' else np.float32
  engine = TrtEngine(str(plan))
  cases = []
  try:
    session = GpuSession(engine, stage / (side + '.onnx'))
    for index in range(2):
      x = arrays[f'input{index}'].astype(dtype)
      cpu = arrays[f'{side}_cpu{index}']
      ref = arrays[f'oracle{index}']
      gpu = session.run(['conv_output'], {'conv_input': x})[0]
      cases.append({'case': index,
                    'gpu_vs_cpu': compare_array(cpu, gpu, side),
                    'gpu_vs_fp64_oracle': compare_array(ref, gpu.astype(np.float64), side),
                    'gpu_vs_rounded_oracle': compare_array(ref.astype(dtype), gpu, side)})
  finally:
    engine.close()
  return {'precision': side, 'cases': cases, 'production_activation_allowed': False}
