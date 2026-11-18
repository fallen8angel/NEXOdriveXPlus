"""Offroad validation and metadata-only registration of an existing GELU plan.

Never builds, installs, selects an engine, or changes current/production caches.
Registered candidates are for a future independent observer, not vehicle control.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

PROFILE = 'nexo-gelu-candidate-v1'


def digest(path):
  from openpilot.tools.jetson.prepare_engine import digest as existing_digest
  return existing_digest(path)


def read_evidence(path):
  with Path(path).open('rb') as stream:
    raw = stream.read(1024 * 1024 + 1)
  if len(raw) > 1024 * 1024:
    raise ValueError('Candidate evidence exceeds bounded report size')
  value = json.loads(raw)
  if not isinstance(value, dict):
    raise ValueError('Invalid candidate evidence record')
  return value, hashlib.sha256(raw).hexdigest()


def read_json(path):
  return read_evidence(path)[0]


def cache_directory(root, directory, prefix):
  cache = Path(root).resolve(strict=True) / 'cache'
  value = Path(directory)
  if value.is_symlink() or value.resolve(strict=True) != value.absolute() or value.parent != cache or not value.name.startswith(prefix):
    raise ValueError('Candidate artifacts must remain in their original DATA cache stage')
  if not value.is_dir():
    raise ValueError('Missing candidate stage')
  return value


def artifact_paths(root, trial):
  trial = Path(trial)
  stage = cache_directory(root, trial.parent, 'nexo-experiment-')
  if trial.name != 'gelu_fp32_island' or trial.is_symlink() or not trial.is_dir():
    raise ValueError('Only the existing full-model gelu_fp32_island trial is supported')
  paths = {'onnx': stage / 'models/gelu_fp32_island.onnx', 'plan': trial / 'experiment.plan',
           'metadata': trial / 'experiment.json', 'trial_result': trial / 'result.json'}
  for path in paths.values():
    if not path.is_file() or path.is_symlink() or path.resolve(strict=True) != path.absolute():
      raise ValueError('Candidate artifact missing or linked outside its stage')
  return paths


def verify_artifacts(root, release, trial, scratch):
  """Bind the exact tested plan and deterministic existing FP32-island transform."""
  import onnx
  from openpilot.tools.jetson import onnx_inputs
  from openpilot.tools.jetson.onnx_contract import verify_pair
  from openpilot.tools.jetson.prepare_engine import MODEL
  from openpilot.tools.jetson.trt_conv_gelu_experiment import make_candidate, MODEL_SHA
  paths = artifact_paths(root, trial)
  row, meta = read_json(paths['trial_result']), read_json(paths['metadata'])
  hashes = {key: digest(path) for key, path in paths.items()}
  if (row.get('name') != 'gelu_fp32_island' or row.get('state') != 'gpu_synthetic_passed'
      or row.get('full_model') is not True or row.get('arithmetic_rewrite') is not True
      or row.get('sha256') != hashes['onnx'] or row.get('plan_sha256') != hashes['plan']
      or meta.get('source_model_sha256') != MODEL_SHA or meta.get('experiment_only') is not True):
    raise ValueError('Existing GPU trial/model/plan evidence mismatch; no build or replacement performed')
  original = Path(release) / MODEL
  pair = verify_pair(original, paths['onnx'], MODEL_SHA, hashes['onnx'])
  parser_inputs = [{**v, 'dtype': 'FLOAT16'} if v['name'] in ('img', 'big_img') and v['dtype'] == 'UINT8' else v
                   for v in pair['original']['inputs']]
  if (pair['candidate']['inputs'] != parser_inputs or pair['candidate']['outputs'] != pair['original']['outputs']
      or not pair['metadata_matches']):
    raise ValueError('Candidate changed the original NEXO I/O or metadata')
  # Recreate ONE temporary ONNX only, not all experiments and never a new plan.
  with tempfile.TemporaryDirectory(prefix='.gelu-identity-', dir=scratch) as temporary:
    transformed = Path(temporary) / 'island.onnx'
    make_candidate(original, transformed)
    expected = onnx_inputs.adapt(onnx.load(transformed)).SerializeToString()
    if hashlib.sha256(expected).hexdigest() != hashes['onnx']:
      raise ValueError('Candidate is not the exact existing FP32 GELU island plus checked input adapter')
  marker = read_json(Path(release) / 'NEXO_RUNTIME.json')
  return paths, {'profile': PROFILE, 'source_model_sha256': MODEL_SHA, 'artifact_sha256': hashes,
                 'artifact_paths': {key: str(path) for key, path in paths.items()},
                 'source_commit': marker['source_commit'], 'runtime': marker['runtime'],
                 'model_contract': pair['original'], 'parser_contract': pair['candidate'],
                 'input_adapter': 'existing_checked_uint8_to_fp16_images', 'original_model': str(original)}


def check_bindings(engine, contract):
  import numpy as np
  import onnx
  for side in ('inputs', 'outputs'):
    expected = {v['name']: v for v in contract[side]}
    actual = getattr(engine, side)
    if set(actual) != set(expected):
      raise ValueError('Candidate TensorRT tensor names differ from the original model')
    for name, item in expected.items():
      dtype = onnx.helper.tensor_dtype_to_np_dtype(getattr(onnx.TensorProto, item['dtype']))
      if tuple(actual[name].shape) != tuple(item['shape']) or np.dtype(actual[name].dtype) != np.dtype(dtype):
        raise ValueError('Candidate TensorRT shape/dtype differs from the original model')


def validate_existing(root, release, stage, trial, samples, engine_factory, reference_factory, progress=None):
  """Called only inside the parent's disposable, continuously Offroad-guarded worker."""
  import numpy as np
  from openpilot.tools.jetson.model_accuracy import validate_cases, feeds_for_case
  from openpilot.tools.jetson.trt_observation import verify_cleanup
  def mark(phase):
    if progress is not None:
      progress(phase)
  mark('candidate_artifact_verification')
  paths, identity = verify_artifacts(root, release, trial, stage)
  original = candidate = engine = None
  try:
    mark('cpu_reference_loading')
    original = reference_factory(identity['original_model'])
    candidate = reference_factory(paths['onnx'])
    mark('gpu_loading')
    engine = engine_factory(str(paths['plan']))
    check_bindings(engine, identity['parser_contract'])
    def gpu(feeds):
      for name, value in feeds.items():
        np.copyto(engine.host_input(name), value)
      return engine.run()
    gpu(feeds_for_case(identity['model_contract'], 0))
    engine.warm()
    mark('numerical_comparison')
    result = validate_cases(original, candidate, gpu, identity['model_contract'], samples)
    result['timing']['timing_scope'] = 'synchronized GPU execution including host input copy and host/device transfers; excludes CPU reference'
  finally:
    # A cleanup failure blocks registration. Release every resource even if one close fails.
    cleanup = []
    for resource in (engine, candidate, original):
      if resource is not None:
        try:
          resource.close()
          if resource is engine:
            verify_cleanup(engine)
        except Exception as error:
          cleanup.append(type(error).__name__)
    if cleanup:
      mark('candidate_cleanup')
      raise RuntimeError('Candidate validation cleanup failed: ' + ','.join(cleanup))
  if any(digest(path) != identity['artifact_sha256'][key] for key, path in paths.items()):
    raise ValueError('Candidate artifact changed during comparison')
  return {**identity, **result, 'state': 'passed' if result['numerical_validation_passed'] else 'accuracy_rejected',
          'error': None if result['numerical_validation_passed'] else 'Numerical differences exceeded the engineering screening policy',
          'failure_code': None if result['numerical_validation_passed'] else 'NUMERICAL_TOLERANCE_EXCEEDED',
          'phase': 'candidate_accuracy', 'gpu_synthetic_verified': True,
          'production_cache_published': False, 'driving_activation_performed': False,
          'vehicle_inference_verified': False, 'vehicle_approved': False}


def worker(root, release, stage, samples, trial):
  import platform
  import sys
  from openpilot.tools.jetson.prepare_engine import store, runtime_preflight
  from openpilot.tools.jetson.engine_experiments import memory_snapshot
  report = {'state': 'running', 'phase': 'candidate_preflight', 'profile': PROFILE,
            'production_cache_published': False, 'driving_activation_performed': False,
            'vehicle_approved': False, 'vehicle_inference_verified': False}
  os.setsid()
  try:
    report['phase'] = 'candidate_artifact_verification'
    paths = artifact_paths(root, trial)
    metadata = read_json(paths['metadata'])
    report['phase'] = 'runtime_imports'
    sys.path[:0] = [str(release), str(Path(release) / 'carrot-server/third_party/jetlink')]
    import tensorrt as trt
    from jetlink.server.backends.trt import engine, build
    from openpilot.tools.jetson.trt_observation import engine_class
    from openpilot.tools.jetson.model_accuracy import CPUReference
    marker = read_json(Path(release) / 'NEXO_RUNTIME.json')
    report['phase'] = 'runtime_abi_check'
    runtime_preflight(marker, sys.version_info[:2], platform.machine(), trt.__version__)
    if metadata.get('trt_version') != trt.__version__ or metadata.get('device') != build.device_tag():
      raise ValueError('Existing candidate TensorRT/GPU ABI differs; rebuild is NOT automatic')
    report.update(phase='candidate_accuracy', memory_before=memory_snapshot(), gpu_device=build.device_tag(),
                  boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    store(stage / 'result.json', report)
    def progress(phase):
      report['phase'] = phase
      store(stage / 'result.json', report)
    report.update(validate_existing(root, release, stage, trial, samples, engine_class(engine), CPUReference, progress))
    report['memory_after'] = memory_snapshot()
  except BaseException as error:
    report.update(state='failed', failed_phase=report['phase'], error=f'{type(error).__name__}: {error}'[:1000],
                  failure_code='CPU_REFERENCE_DEPENDENCY_MISSING' if isinstance(error, ModuleNotFoundError)
                    and error.name == 'onnxruntime' else 'CANDIDATE_VALIDATION_FAILED',
                  numerical_validation_passed=False)
  finally:
    store(stage / 'result.json', report)


def register(root, validation_stage, offroad_check):
  """Register only a reference receipt; never publish under the source model cache key."""
  import fcntl
  from openpilot.tools.jetson.prepare_engine import MODEL
  from openpilot.tools.jetson.model_accuracy import POLICY, CASES, SLICES
  root = Path(root).resolve(strict=True)
  stage = cache_directory(root, validation_stage, 'nexo-validation-')
  with (root / 'cache/nexo-engine-preparation.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if offroad_check() is not True:
      raise RuntimeError('Actual Offroad state required for candidate registration')
    release = (root / 'current').resolve(strict=True)
    config = root / 'updates/nexo-ncm.json'
    config_bytes = config.read_bytes()
    settings = json.loads(config_bytes)
    if settings.get('display_only') is not True or settings.get('transport') != 'ncm-tcp':
      raise ValueError('Existing Display Only configuration is required')
    if (stage / 'result.json').is_symlink():
      raise ValueError('Validation evidence cannot be a symlink')
    proof, proof_sha = read_evidence(stage / 'result.json')
    supervisor = read_json(root / 'cache/nexo-engine-validation.json')
    if (supervisor.get('state') != 'validation_complete' or supervisor.get('stage_directory') != str(stage)
        or supervisor.get('validation_result_sha256') != proof_sha or supervisor.get('worker_exitcode') != 0):
      raise ValueError('Successful Offroad supervisor receipt required; child result alone cannot register')
    marker = read_json(release / 'NEXO_RUNTIME.json')
    if (proof.get('profile') != PROFILE or proof.get('state') != 'passed'
        or proof.get('numerical_validation_passed') is not True or proof.get('gpu_synthetic_verified') is not True
        or proof.get('policy') != POLICY or proof.get('case_count') != CASES
        or proof.get('dataset_scope') != 'synthetic_v1_not_recorded_driving'
        or proof.get('source_commit') != marker.get('source_commit') or proof.get('runtime') != marker.get('runtime')
        or proof.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        or digest(release / MODEL) != proof.get('source_model_sha256')
        or any(proof.get(k) is not False for k in ('production_cache_published', 'driving_activation_performed', 'vehicle_approved'))):
      raise ValueError('Complete current-release numeric/GPU validation required; no candidate registered')
    if (set(proof.get('worst_by_slice', {})) != {'candidate_cpu_vs_original_cpu', 'candidate_gpu_vs_original_cpu', 'candidate_gpu_vs_candidate_cpu'}
        or len(proof.get('cases', [])) != CASES):
      raise ValueError('Incomplete numerical comparison evidence')
    for groups in proof['worst_by_slice'].values():
      if set(groups) != set(SLICES) or any(m.get('passed') is not True or m.get('outside_tolerance') != 0 for m in groups.values()):
        raise ValueError('Numerical comparison contains failures')
    paths = artifact_paths(root, Path(proof['artifact_paths']['plan']).parent)
    if any(str(path) != proof['artifact_paths'].get(k) or digest(path) != proof['artifact_sha256'].get(k) for k, path in paths.items()):
      raise ValueError('Validated candidate changed; old engine and registry preserved')
    receipt = {'schema_version': 1, 'profile': PROFILE, 'candidate_id': proof_sha,
               'status': 'synthetic_validated_observation_candidate', 'source_model_sha256': proof['source_model_sha256'],
               'artifact_paths': proof['artifact_paths'], 'artifact_sha256': proof['artifact_sha256'],
               'validation_report': str(stage / 'result.json'), 'validation_sha256': proof_sha,
               'source_commit': proof['source_commit'], 'runtime': proof['runtime'], 'policy': POLICY,
               'vehicle_approved': False, 'active': False, 'production_cache_published': False,
               'recorded_driving_verified': False, 'driving_activation_performed': False}
    directory = root / 'cache/engine-candidates'
    if directory.is_symlink():
      raise ValueError('Candidate registry cannot be a symlink')
    directory.mkdir(exist_ok=True)
    target = directory / (proof_sha + '.json')
    if target.exists() or target.is_symlink():
      if not target.is_symlink() and read_json(target) == receipt:
        return receipt
      raise FileExistsError('Existing candidate receipt preserved')
    temporary = None
    try:
      with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.candidate-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
      if (offroad_check() is not True or (root / 'current').resolve() != release or config.read_bytes() != config_bytes
          or digest(stage / 'result.json') != proof_sha
          or any(digest(path) != proof['artifact_sha256'][key] for key, path in paths.items())):
        raise RuntimeError('Offroad/release/configuration/evidence changed before registration')
      os.link(temporary, target)  # Exclusive metadata publication; no engine copies or replacement.
      try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
          os.fsync(descriptor)
        finally:
          os.close(descriptor)
      except BaseException:
        if target.stat().st_ino == temporary.stat().st_ino:
          target.unlink()
        raise
    finally:
      if temporary is not None:
        temporary.unlink(missing_ok=True)
    return receipt
