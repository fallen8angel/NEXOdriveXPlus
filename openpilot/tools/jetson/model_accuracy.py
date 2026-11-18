"""Bounded original-ONNX CPU reference checks, independent of TensorRT builds.

Thresholds are an engineering screening policy, never vehicle-control approval.
No inputs, outputs, weights or running model are persisted or changed here.
"""
import hashlib
import time

POLICY = {'id': 'nexo-gelu-observation-v1', 'atol': .005, 'rtol': .005,
          'vehicle_acceptance_policy': False}
SLICES = {'outputs': (0, 2576), 'lead': (917, 1061), 'lead_prob': (1061, 1064),
          'hidden_state': (1064, 1576), 'plan': (1576, 2566)}
CASES = 8


class CPUReference:
  """Execute the unchanged graph with explicit CPU provider and no graph fusion."""
  def __init__(self, path):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    self.session = ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])
    self.names = [v.name for v in self.session.get_outputs()]
    self.fp16_images = {v.name for v in self.session.get_inputs()
                        if v.name in ('img', 'big_img') and v.type == 'tensor(float16)'}
    self.evidence = {'backend': 'onnxruntime', 'version': ort.__version__, 'provider': 'CPUExecutionProvider',
                     'graph_optimization': 'disabled', 'original_gpu_reference': False,
                     'checked_uint8_to_fp16_images': sorted(self.fp16_images)}

  def run(self, feeds):
    import numpy as np
    # The existing TensorRT parser adapter has FP16 image bindings. Preserve
    # source UINT8 values exactly rather than changing the public model input.
    adapted = {k: v.astype(np.float16) if k in self.fp16_images and v.dtype == np.uint8 else v for k, v in feeds.items()}
    return dict(zip(self.names, self.session.run(self.names, adapted), strict=True))

  def close(self):
    self.session = None


def feeds_for_case(contract, index):
  """Diverse but synthetic frames/contexts; never described as recorded driving."""
  import numpy as np
  import onnx
  if type(index) is not int or not 0 <= index < CASES:
    raise ValueError('Unknown bounded synthetic case')
  rng = np.random.default_rng(20261118 + index)
  feeds = {}
  for item in sorted(contract['inputs'], key=lambda v: v['name']):
    name, shape = item['name'], tuple(item['shape'])
    dtype = onnx.helper.tensor_dtype_to_np_dtype(getattr(onnx.TensorProto, item['dtype']))
    if name in ('img', 'big_img'):
      values = np.full(shape, 0 if index == 0 else 255) if index < 2 else rng.integers(0, 256, shape)
    elif name == 'features_buffer':
      values = np.zeros(shape) if index == 0 else rng.uniform(-1, 1, shape)
    elif name == 'desire_pulse':
      values = np.zeros(shape)
      values[..., index % shape[-1]] = 1
    elif name == 'traffic_convention':
      values = np.zeros(shape)
      values[..., index % 2] = 1
    elif name == 'action_t':
      values = np.full(shape, 0 if index == 0 else (index / CASES))
    else:
      raise ValueError('Unexpected NEXO model input')
    feeds[name] = np.asarray(values, dtype=dtype)
    feeds[name].setflags(write=False)
  return feeds


def input_digest(feeds):
  """Bind names, dtype, shape and exact shared inputs without recording images."""
  digest = hashlib.sha256()
  for name, value in sorted(feeds.items()):
    digest.update(f'{name}:{value.dtype}:{value.shape}\0'.encode())
    digest.update(value.tobytes(order='C'))
  return digest.hexdigest()


def comparison(observed, reference):
  import numpy as np
  from openpilot.tools.jetson.onnx_contract import validate_outputs
  from openpilot.tools.jetson.engine_experiments import compare_outputs
  for values in (observed, reference):
    validate_outputs(values, {'outputs': (1, 2576)}, {'outputs': np.float16})
  result = {}
  for label, (start, end) in SLICES.items():
    value, expected = observed['outputs'][:, start:end], reference['outputs'][:, start:end]
    delta = np.abs(value.astype(np.float64) - expected.astype(np.float64))
    limit = POLICY['atol'] + POLICY['rtol'] * np.abs(expected.astype(np.float64))
    base = compare_outputs({'outputs': value}, {'outputs': expected})['outputs']
    failures = int(np.count_nonzero(delta > limit))
    result[label] = {**base, 'p95_abs_error': float(np.percentile(delta, 95)),
                     'max_tolerance_ratio': float(np.max(delta / limit)),
                     'outside_tolerance': failures, 'passed': failures == 0}
  return result


def validate_cases(original, candidate, gpu, contract, samples=20):
  """Three-way comparison on identical inputs; all cases must pass, no averaging away failures."""
  import numpy as np
  from openpilot.tools.jetson.onnx_contract import benchmark_engine
  comparisons = ('candidate_cpu_vs_original_cpu', 'candidate_gpu_vs_original_cpu', 'candidate_gpu_vs_candidate_cpu')
  rows, worst = [], {key: {} for key in comparisons}
  for index in range(CASES):
    feeds = feeds_for_case(contract, index)
    feed_sha = input_digest(feeds)
    reference = {k: v.copy() for k, v in original.run(feeds).items()}
    expected = {k: v.copy() for k, v in candidate.run(feeds).items()}
    actual = {k: v.copy() for k, v in gpu(feeds).items()}
    if input_digest(feeds) != feed_sha:
      raise ValueError('A validation backend changed the shared comparison inputs')
    row = {'case': index, 'input_sha256': feed_sha}
    for name, values in zip(comparisons, ((expected, reference), (actual, reference), (actual, expected)), strict=True):
      row[name] = comparison(*values)
      for label, metrics in row[name].items():
        aggregate = worst[name].setdefault(label, {'passed': True, 'outside_tolerance': 0})
        aggregate['passed'] &= metrics['passed']
        aggregate['outside_tolerance'] += metrics['outside_tolerance']
        for key in ('max_abs_error', 'rmse', 'p95_abs_error', 'max_tolerance_ratio'):
          aggregate[key] = max(aggregate.get(key, 0.), metrics[key])
    rows.append(row)
  # Reuse the existing synchronized benchmark, varying inputs every measured run.
  # References are excluded from timing. GPU callable must synchronize engine.run().
  current = {}
  def prepare(index):
    current.clear()
    current.update(feeds_for_case(contract, index % CASES))
  class Engine:
    def run(self):
      return gpu(current)
  timing = benchmark_engine(Engine(), prepare, {'outputs': (1, 2576)}, {'outputs': np.float16}, samples=samples)
  passed = all(metrics['passed'] for groups in worst.values() for metrics in groups.values())
  return {'numerical_validation_passed': passed, 'policy': dict(POLICY), 'case_count': CASES,
          'dataset_scope': 'synthetic_v1_not_recorded_driving', 'cases': rows, 'worst_by_slice': worst,
          'reference': dict(original.evidence), 'candidate_cpu_reference': dict(candidate.evidence),
          'timing': timing, 'synthetic_fps': 1000 / timing['synchronized_inference_ms']['mean']
              if timing['synchronized_inference_ms']['mean'] > 0 else None,
          'numerical_equivalence_verified': False, 'vehicle_approved': False,
          'validated_monotonic': time.monotonic()}
