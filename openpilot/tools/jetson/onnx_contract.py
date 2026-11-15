"""Offline integrity and exact I/O checks. Never builds, installs or selects a model."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time


def _graph_tensors(graph):
  import onnx
  yield from graph.initializer
  for item in graph.sparse_initializer:
    yield item.values
    yield item.indices
  for node in graph.node:
    for attr in node.attribute:
      if attr.type == onnx.AttributeProto.TENSOR:
        yield attr.t
      elif attr.type == onnx.AttributeProto.TENSORS:
        yield from attr.tensors
      elif attr.type == onnx.AttributeProto.GRAPH:
        yield from _graph_tensors(attr.g)
      elif attr.type == onnx.AttributeProto.GRAPHS:
        for child in attr.graphs:
          yield from _graph_tensors(child)


def inspect_model(path, expected_sha256):
  """Hashes must come from trusted provenance, not from the file being inspected."""
  import onnx
  from onnx.external_data_helper import uses_external_data
  path = Path(path)
  if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
    raise ValueError('Trusted model SHA-256 is required')
  raw = path.read_bytes()
  actual = hashlib.sha256(raw).hexdigest()
  if actual != expected_sha256:
    raise ValueError(f'Model SHA-256 mismatch: {path.name}')
  model = onnx.load_model_from_string(raw)
  if any(uses_external_data(tensor) for tensor in _graph_tensors(model.graph)):
    raise ValueError('External tensor files are not covered by the model digest')
  onnx.checker.check_model(model, full_check=True)

  def interface(values):
    result = []
    names = set()
    for value in values:
      if not value.name or value.name in names or not value.type.HasField('tensor_type'):
        raise ValueError('Invalid or duplicate model interface name/type')
      names.add(value.name)
      tensor = value.type.tensor_type
      shape = [dimension.dim_value for dimension in tensor.shape.dim]
      if not tensor.HasField('shape') or any(d <= 0 for d in shape):
        raise ValueError('Static positive tensor dimensions required')
      result.append({'name': value.name, 'dtype': onnx.TensorProto.DataType.Name(tensor.elem_type), 'shape': shape})
    if not result:
      raise ValueError('Empty model interface')
    return result

  return {'sha256': actual, 'nbytes': len(raw), 'inputs': interface(model.graph.input),
          'outputs': interface(model.graph.output),
          'metadata': {value.key: value.value for value in model.metadata_props}}


def verify_pair(original, candidate, original_sha256, candidate_sha256):
  source = inspect_model(original, original_sha256)
  patched = inspect_model(candidate, candidate_sha256)
  differences = []
  for key in ('inputs', 'outputs', 'metadata'):
    if source[key] != patched[key]:
      differences.append(key)
  return {'schema_version': 1, 'read_only': True, 'original': source, 'candidate': patched,
          'exact_io_compatible': source['inputs'] == patched['inputs'] and source['outputs'] == patched['outputs'],
          'metadata_matches': source['metadata'] == patched['metadata'], 'differences': differences,
          'state': 'contract_matches' if not differences else 'contract_mismatch',
          'numerical_equivalence_verified': False, 'tensorrt_engine_verified': False,
          'vehicle_approved': False,
          'note': 'I/O compatibility does not prove arithmetic equivalence or suitability for vehicle control.'}


def validate_outputs(values, shapes, dtypes=None):
  import numpy as np
  if (not isinstance(values, dict) or set(values) != set(shapes)
      or any(not isinstance(value, np.ndarray) or value.shape != tuple(shapes[name])
             or value.dtype != np.dtype(dtypes[name] if dtypes is not None else np.float32)
             or not np.isfinite(value).all() for name, value in values.items())):
    raise ValueError('NEXO TensorRT output shape/dtype/finite contract failed')


def benchmark_engine(engine, prepare, shapes, dtypes, warmups=3, samples=20):
  """Time synchronized engine.run(), including transfers; never a kernel-only claim."""
  import numpy as np
  if type(warmups) is not int or type(samples) is not int or not 1 <= warmups <= 20 or not 2 <= samples <= 100:
    raise ValueError('Synthetic inference sample budget invalid')
  durations = []
  for index in range(warmups + samples):
    prepare(index)
    started = time.perf_counter()
    values = engine.run()  # Pinned TrtEngine.run() synchronizes its CUDA stream.
    elapsed = (time.perf_counter() - started) * 1000
    validate_outputs(values, shapes, dtypes)
    if not np.isfinite(elapsed) or elapsed < 0:
      raise ValueError('Invalid synchronized inference timing')
    if index >= warmups:
      durations.append(elapsed)
  return {'warmup_runs': warmups, 'measured_runs': samples,
          'synchronized_inference_ms': {
            'mean': float(np.mean(durations)), 'p50': float(np.percentile(durations, 50)),
            'p95': float(np.percentile(durations, 95)), 'min': min(durations), 'max': max(durations)},
          'timing_scope': 'synchronized engine.run including host/device transfers; not kernel-only',
          'numerical_equivalence_verified': False, 'vehicle_inference_verified': False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--original', type=Path, required=True)
  parser.add_argument('--candidate', type=Path, required=True)
  parser.add_argument('--original-sha256', required=True)
  parser.add_argument('--candidate-sha256', required=True)
  args = parser.parse_args()
  try:
    report = verify_pair(args.original, args.candidate, args.original_sha256, args.candidate_sha256)
  except Exception as error:
    print(json.dumps({'state': 'failed', 'phase': 'onnx_integrity_and_contract',
                      'error': f'{type(error).__name__}: {error}', 'vehicle_approved': False}))
    return 1
  print(json.dumps(report, indent=2))
  return 0 if report['state'] == 'contract_matches' else 1


if __name__ == '__main__':
  raise SystemExit(main())
