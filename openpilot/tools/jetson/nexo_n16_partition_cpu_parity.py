"""Offroad-gated Carrot N16 CPU parity helper. No TensorRT, CAN, camera, HUD.

Only call from the protected trial parent, which holds engine lock, samples
memory/disk and rechecks Comma4 Offroad. Subworkers exit between models.
"""
from __future__ import annotations

import hashlib
import importlib
import multiprocessing
from pathlib import Path
import sys

from openpilot.tools.jetson.nexo_n16_partition_extract import verify_exported_halves

CASES = 2
MAX_ARCHIVE = 128 << 20
MAX_ARRAY = 32 << 20
MAX_ELEMENTS = 6_000_000
POLICY = {'id': 'n16-synthetic-cpu-v1', 'atol': 0.005, 'rtol': 0.005,
          'vehicle_acceptance_policy': False}
# Never pip-install into the protected Jetson service venv. An explicit,
# offline CPU-only package may be staged under this DATA cache directory
# and used only by guarded synthetic parity workers. Missing dependencies
# remain a blocker, not a successful parity result.
ORT_CACHE = Path('/opt/carrot-jetlink/cache/nexo-n16-cpu-ort')


def load_cpu_ort(cache: Path = ORT_CACHE):
  """Require an importable CPUExecutionProvider without modifying system Python.

  A sidecar --target installation is allowed only at a fixed DATA location;
  missing, malformed, or incompatible libraries always fail closed.
  """
  try:
    ort = importlib.import_module('onnxruntime')
  except ModuleNotFoundError as error:
    if error.name != 'onnxruntime':
      raise RuntimeError('CPU ONNX Runtime dependency missing: ' + str(error.name)) from error
    if (cache.is_symlink() or not cache.is_dir()
        or not (cache / 'onnxruntime' / '__init__.py').is_file()
        or (cache / 'onnxruntime').is_symlink()):
      raise RuntimeError(
        'CPU ONNX Runtime unavailable. No system/venv changes made; '
        'inspect the pinned isolated CPU wheel before retrying') from error
    # Append, do not prepend: existing numpy, protobuf and scipy in the
    # protected venv are not shadowed by unrelated downloaded packages.
    if str(cache) not in sys.path:
      sys.path.append(str(cache))
    try:
      ort = importlib.import_module('onnxruntime')
    except (ModuleNotFoundError, ImportError, OSError) as secondary:
      raise RuntimeError('Isolated CPU ONNX Runtime cannot import: ' +
                         str(secondary)[:240]) from secondary
  except (ImportError, OSError) as error:
    raise RuntimeError('CPU ONNX Runtime cannot import: ' + str(error)[:240]) from error
  if 'CPUExecutionProvider' not in ort.get_available_providers():
    raise RuntimeError('CPUExecutionProvider absent; parity not supported')
  return ort


SLICES = {'lane_lines': (0, 528), 'lane_lines_prob': (528, 536),
          'road_edges': (536, 800), 'plan': (917, 1907),
          'lead': (1907, 2051), 'lead_prob': (2051, 2054)}


def digest(path: Path) -> str:
  sha = hashlib.sha256()
  with path.open('rb') as stream:
    for data in iter(lambda: stream.read(4 << 20), b''):
      sha.update(data)
  return sha.hexdigest()


def ort_dtype(name: str):
  import numpy as np
  kinds = {'tensor(float16)': np.float16, 'tensor(float)': np.float32,
           'tensor(double)': np.float64, 'tensor(uint8)': np.uint8,
           'tensor(int8)': np.int8, 'tensor(int32)': np.int32,
           'tensor(int64)': np.int64, 'tensor(bool)': np.bool_}
  if name not in kinds:
    raise ValueError('Unknown CPU input dtype: ' + name[:80])
  return np.dtype(kinds[name])


def synthetic_feeds(inputs, index: int) -> dict:
  import numpy as np
  if type(index) is not int or not 0 <= index < CASES:
    raise ValueError('Invalid CPU synthetic case')
  result = {}
  for item in sorted(inputs, key=lambda x: x.name):
    dims = item.shape
    if (not isinstance(dims, (list, tuple)) or not dims or
        any(type(v) is not int or v <= 0 for v in dims)):
      raise ValueError('Dynamic tensor shape: ' + item.name)
    dtype = ort_dtype(item.type)
    count = 1
    for dim in dims:
      count *= dim
    if count > MAX_ELEMENTS or count*dtype.itemsize > MAX_ARRAY:
      raise ValueError('Excessive N16 input: ' + item.name)
    shape = tuple(dims)
    seed = int.from_bytes(hashlib.sha256(item.name.encode()).digest()[:4], 'big')
    rng = np.random.default_rng(20261121 + seed + 101*index)
    if index == 0:
      arr = np.zeros(shape, dtype=dtype)
    elif item.name in ('new_img', 'state_img_q'):
      # Every UINT8 source pixel (0..255) is exactly representable in FP16.
      arr = rng.integers(0, 256, shape, dtype=np.uint16).astype(dtype)
    elif dtype.kind == 'b':
      arr = np.zeros(shape, dtype=dtype)
    elif dtype.kind in 'ui':
      arr = rng.integers(0, 2, shape, dtype=dtype)
    elif item.name in ('desire', 'traffic_convention'):
      arr = np.zeros(shape, dtype=dtype)
      arr.reshape(-1)[min(index, arr.size-1)] = 1
    else:
      arr = rng.uniform(-0.125, 0.125, shape).astype(dtype)
    if not np.isfinite(arr).all():
      raise ValueError('Nonfinite synthetic input')
    result[item.name] = arr
  return result


def save_arrays(path: Path, data: dict) -> None:
  import numpy as np
  if path.exists() or path.is_symlink():
    raise ValueError('Refusing overwrite of N16 trial data')
  if not 0 < len(data) <= 64 or any(
      not isinstance(name, str) or not name or
      not isinstance(value, np.ndarray) or value.nbytes > MAX_ARRAY
      for name, value in data.items()):
    raise ValueError('Unbounded N16 trial data')
  np.savez(path, **data)
  if path.stat().st_size > MAX_ARCHIVE:
    raise ValueError('N16 trial output is too large')


def load_arrays(path: Path) -> dict:
  import numpy as np
  if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
    raise ValueError('Untrusted N16 trial data: ' + path.name)
  with np.load(path, allow_pickle=False) as src:
    if not 0 < len(src.files) <= 64 or len(set(src.files)) != len(src.files):
      raise ValueError('Unbounded N16 archive')
    data = {k: src[k] for k in src.files}
  if any(value.nbytes > MAX_ARRAY or not np.isfinite(value).all() for value in data.values()):
    raise ValueError('Nonfinite or excessive N16 trial array')
  return data


def bind_inputs(inputs, available: dict) -> dict:
  import numpy as np
  feeds = {}
  for item in inputs:
    v = available.get(item.name)
    if (not isinstance(v, np.ndarray) or tuple(v.shape) != tuple(item.shape)
        or v.dtype != ort_dtype(item.type) or v.size > MAX_ELEMENTS
        or not np.isfinite(v).all()):
      raise ValueError('Unmatched N16 partition input: ' + item.name)
    feeds[item.name] = v
  return feeds


def cpu_step(which: str, path: Path, stage: Path) -> None:
  """One CPU model session per process, two immutable synthetic test cases."""
  ort = load_cpu_ort()
  from openpilot.tools.jetson.nexo_n16_registry import EXPECTED_INPUTS, EXPECTED_OUTPUTS
  if which not in ('reference', 'left', 'right') or path.is_symlink() or not path.is_file():
    raise ValueError('Invalid CPU parity step')
  opts = ort.SessionOptions()
  opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
  opts.intra_op_num_threads = opts.inter_op_num_threads = 1
  opts.enable_cpu_mem_arena = False
  opts.enable_mem_pattern = False
  sess = ort.InferenceSession(str(path), sess_options=opts,
                              providers=['CPUExecutionProvider'])
  if sess.get_providers() != ['CPUExecutionProvider']:
    raise ValueError('CPU-only session unavailable')
  inputs, outputs = sess.get_inputs(), sess.get_outputs()
  if which == 'reference' and (
      {x.name for x in inputs} != EXPECTED_INPUTS or
      {x.name for x in outputs} != EXPECTED_OUTPUTS):
    raise ValueError('Original pinned N16 tensor names changed')
  outnames = [v.name for v in outputs]
  for case in range(CASES):
    if which == 'reference':
      all_inputs = synthetic_feeds(inputs, case)
      save_arrays(stage / f'inputs-{case}.npz', all_inputs)
    else:
      all_inputs = load_arrays(stage / f'inputs-{case}.npz')
      if which == 'right':
        intermediate = load_arrays(stage / f'left-{case}.npz')
        if set(intermediate) & set(all_inputs):
          raise ValueError('Boundary shadows original input')
        all_inputs.update(intermediate)
    result = sess.run(outnames, bind_inputs(inputs, all_inputs))
    save_arrays(stage / f'{which}-{case}.npz',
                dict(zip(outnames, result, strict=True)))
  del sess


def compare_array(expected, actual, name: str) -> dict:
  import numpy as np
  if expected.shape != actual.shape or expected.dtype != actual.dtype:
    raise ValueError('N16 parity shape/dtype changed: ' + name)
  if expected.size == 0 or not np.isfinite(expected).all() or not np.isfinite(actual).all():
    raise ValueError('N16 parity output empty or nonfinite: ' + name)
  x, y = expected.astype(np.float64), actual.astype(np.float64)
  diff = np.abs(x-y)
  tolerance = POLICY['atol'] + POLICY['rtol']*np.abs(x)
  fail = int(np.count_nonzero(diff > tolerance))
  return {'shape': list(expected.shape), 'dtype': str(expected.dtype),
          'elements': int(expected.size), 'outside_tolerance': fail,
          'max_abs_error': float(diff.max()),
          'p95_abs_error': float(np.percentile(diff, 95)),
          'rmse': float(np.sqrt(np.mean(diff*diff))), 'passed': fail == 0}


def compare_cases(stage: Path, expected_outputs: dict) -> dict:
  records, failed = [], []
  for case in range(CASES):
    original = load_arrays(stage / f'reference-{case}.npz')
    left = load_arrays(stage / f'left-{case}.npz')
    right = load_arrays(stage / f'right-{case}.npz')
    if set(original) != set(expected_outputs) or set(left) & set(right):
      raise ValueError('Original output contract or partition output ownership changed')
    combined = {**left, **right}
    if not set(original) <= set(combined):
      raise ValueError('Partition outputs omit original tensor')
    row = {'case': case,
           'input_archive_sha256': digest(stage / f'inputs-{case}.npz'),
           'outputs': {}}
    for name, shape in expected_outputs.items():
      if list(original[name].shape) != shape:
        raise ValueError('Pinned N16 output shape changed: ' + name)
      metric = compare_array(original[name], combined[name], name)
      row['outputs'][name] = metric
      if not metric['passed']:
        failed.append(f'{case}:{name}')
    for label, (start, end) in SLICES.items():
      metric = compare_array(original['outputs'][:, start:end],
                             combined['outputs'][:, start:end], label)
      row['outputs']['outputs:' + label] = metric
      if not metric['passed']:
        failed.append(f'{case}:{label}')
    records.append(row)
  good = not failed
  return {'state': 'cpu_parity_passed' if good else 'cpu_parity_failed',
          'phase': 'onnx_partition_cpu_parity',
          'dataset_scope': 'two_deterministic_synthetic_cases_cpu_only',
          'case_count': CASES, 'policy': dict(POLICY),
          'reference_provider': 'CPUExecutionProvider',
          'graph_optimizations': 'disabled',
          'failed_outputs': failed[:48], 'cases': records,
          'numerical_parity_verified': good, 'cpu_numerical_parity_verified': good,
          'gpu_inference_verified': False, 'engine_build_attempted': False,
          'vehicle_control_enabled': False, 'hud_prediction_enabled': False,
          'real_vehicle_validated': False, 'production_engine_published': False}


def cpu_step_child(which: str, path: Path, stage: Path):
  try:
    cpu_step(which, path, stage)
  except BaseException as error:
    from openpilot.tools.jetson.nexo_n16_diagnostics import save_json
    save_json(stage / (which + '-failure.json'),
              {'error': f'{type(error).__name__}: {error}'[:500]})
    raise


def run_cpu_parity(release: Path, stage: Path, on_phase) -> dict:
  """Parent call is protected by CommaGuard+resource monitoring in trt_trial."""
  from openpilot.tools.jetson.nexo_n16_partition_extract import N16_CUT
  from openpilot.tools.jetson.nexo_n16_trt_trial import (
    MODEL_SHA, MODEL_BYTES, ROOT, _prepatch_worker)
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  from openpilot.tools.jetson.nexo_n16_registry import load_registry
  target = ROOT / 'cache' / ('nexo-n16-partition-export-' + MODEL_SHA[:12])
  if target.is_symlink() or not target.is_dir():
    raise ValueError('The verified N16 split models are missing')
  receipt = verify_exported_halves(target, N16_CUT)
  if (receipt['source_sha256'] != MODEL_SHA or
      receipt['left_sha256'] != digest(target / 'left.onnx') or
      receipt['right_sha256'] != digest(target / 'right.onnx')):
    raise ValueError('N16 split model hashes disagree with verified export')
  spec, _ = load_registry()
  if spec['sha256'] != MODEL_SHA or spec['nbytes'] != MODEL_BYTES:
    raise ValueError('Pinned N16 original model changed')
  # Fail before generating an 815 MB temporary model when CPU ORT is absent.
  # This check has no writes, downloads, pip installs or vehicle effects.
  on_phase('n16_cpu_runtime_preflight')
  load_cpu_ort()
  on_phase('n16_original_cpu_prepare')
  proc = multiprocessing.get_context('spawn').Process(
    target=_prepatch_worker, args=(release, stage))
  proc.start()
  proc.join()
  check = read_report(stage / 'prepatch-status.json')
  if proc.exitcode != 0 or check.get('ok') is not True:
    raise RuntimeError('N16 original CPU preparation failed: ' +
                       str(check.get('error', 'worker terminated'))[:270])
  original = stage / 'n16-prepatched.onnx'
  if original.is_symlink() or not original.is_file() or original.stat().st_size < 400_000_000:
    raise ValueError('Original N16 temporary CPU reference is missing')
  for which, model in (('reference', original),
                       ('left', target / 'left.onnx'),
                       ('right', target / 'right.onnx')):
    on_phase('n16_cpu_' + which)
    child = multiprocessing.get_context('spawn').Process(
      target=cpu_step_child, args=(which, model, stage))
    child.start()
    child.join()
    if child.exitcode != 0:
      details = read_report(stage / (which + '-failure.json'))
      raise RuntimeError('N16 CPU ' + which + ' failed: ' +
                         str(details.get('error', 'worker terminated'))[:300])
  on_phase('n16_cpu_compare')
  return {**compare_cases(stage, spec['output_shapes']), 'model_sha256': MODEL_SHA,
          'left_sha256': receipt['left_sha256'],
          'right_sha256': receipt['right_sha256']}
