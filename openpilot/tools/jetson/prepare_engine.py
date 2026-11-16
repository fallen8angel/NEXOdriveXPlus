"""Explicit Offroad engine preparation, independent of HUD and driving activation.

Run on Jetson with its installed protected runtime. A read-only SSH check of
Comma's two Offroad flags guards the entire child build. Never changes current,
NCM configuration, services, last-loaded.json, Params or the Jetlink enable flag.
"""
import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path('/opt/carrot-jetlink')
MODEL = 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
OFFROAD_SCRIPT = """from pathlib import Path
import json
v = {k: Path('/data/params/d', k).read_text().strip() for k in ('IsOffroad', 'IsOnroad')}
try:
  Path('/data/nexo_jetlink_enabled').lstat()
  v['JetlinkEnabled'] = True
except FileNotFoundError:
  v['JetlinkEnabled'] = False
print(json.dumps(v))
"""


def digest(path):
  sha = hashlib.sha256()
  with Path(path).open('rb') as source:
    while chunk := source.read(1024 * 1024):
      sha.update(chunk)
  return sha.hexdigest()


def store(path, value):
  temporary = path.with_suffix('.tmp')
  temporary.write_text(json.dumps(value, allow_nan=False, indent=2))
  temporary.replace(path)


def comma_offroad(target):
  # Fixed USB peer, no arbitrary remote command, credentials, provisioning or shell interpolation.
  if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*@192\.168\.100\.127', target):
    raise ValueError('Use the existing Comma USB-NCM SSH user@192.168.100.127')
  result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                           '-o', 'ConnectTimeout=3', '--', target, 'python3', '-'],
                          input=OFFROAD_SCRIPT, capture_output=True, text=True, timeout=5, check=True)
  value = json.loads(result.stdout)
  return (value.get('IsOffroad') == '1' and value.get('IsOnroad') == '0'
          and value.get('JetlinkEnabled') is False)



def runtime_preflight(marker, python_version, architecture, trt_version):
  expected = marker.get('runtime') or {}
  if (tuple(python_version) != (3, 10) or architecture != 'aarch64'
      or expected.get('arch') != architecture or trt_version != '10.3.0'
      or expected.get('tensorrt') != trt_version):
    raise ValueError('Python/architecture/TensorRT differs from the protected runtime ABI')

def worker(root, release, stage, samples=100):
  """Only this disposable process imports CUDA; every write stays in stage."""
  os.setsid()
  report_path = stage / 'result.json'
  result = {'state': 'running', 'phase': 'imports', 'vehicle_inference_verified': False,
            'driving_activation_performed': False, 'synthetic_inference': False}
  host = None
  try:
    vendor = release / 'carrot-server'
    sys.path[:0] = [str(release), str(vendor / 'third_party/jetlink')]
    from openpilot.tools.jetson.onnx_inputs import install_carrot_patch, preflight
    from openpilot.tools.jetson.onnx_contract import inspect_model, benchmark_engine
    from openpilot.tools.jetson.trt_diagnostics import install
    from openpilot.tools.jetson.trt_observation import backend_class, verify_cleanup, PROFILE
    from jetlink.spec import spec_from_onnx
    from jetlink.server.cache import EngineCache
    from jetlink.server.session import EngineHost, Request
    import numpy as np
    import onnx
    import tensorrt
    marker = json.loads((release / 'NEXO_RUNTIME.json').read_text())
    runtime_preflight(marker, sys.version_info[:2], platform.machine(), tensorrt.__version__)
    model = release / MODEL
    spec = spec_from_onnx(str(model), frame_skip=4).to_dict()
    if spec != marker['model']:
      raise ValueError('Installed ONNX and package model contract differ')
    contract = inspect_model(model, spec['sha256'])
    install_carrot_patch()
    preflight(model, spec['sha256'])
    backend = backend_class(install())  # Original builder/math, checked CUDA resource recovery in this worker only.
    result['observation_runtime_profile'] = PROFILE
    cache = EngineCache(stage / 'cache', backend())
    shutil.copyfile(model, cache.model_path(spec['sha256']))
    entry = cache.entry(spec['sha256'])
    previous = root / 'cache/engines' / entry.path.name
    if previous.exists() or previous.with_suffix('.json').exists():
      meta = previous.with_suffix('.json')
      if previous.is_symlink() or meta.is_symlink() or json.loads(meta.read_text()).get('spec') != spec:
        raise ValueError('Existing cache contract is invalid; original files preserved')
      shutil.copyfile(previous, entry.path)
      shutil.copyfile(meta, entry.meta_path)
      result['artifact_origin'] = 'existing_cache'
    else:
      result['artifact_origin'] = 'new_build'
    host = EngineHost(cache)
    result.update(phase='engine_build_or_load', model_sha256=spec['sha256'])
    store(report_path, result)
    host.request(Request(spec['sha256'], spec['nbytes'], 4), None)
    while True:  # Parent enforces time limit and continuously checks actual Offroad state.
      state = host.status(spec['sha256'], 4)
      if state['state'] == 'ready':
        break
      if state['state'] not in ('building', 'loading'):
        raise RuntimeError(str(state))
      time.sleep(.2)
    loaded = host.loaded
    if loaded.spec.to_dict() != spec:
      raise ValueError('Loaded engine contract differs from installed model')
    dtypes = {item['name']: onnx.helper.tensor_dtype_to_np_dtype(getattr(onnx.TensorProto, item['dtype']))
              for item in contract['outputs']}
    rng = np.random.default_rng(20261115)
    loaded.queues.reset()
    def prepare(index):
      loaded.queues.step_into(rng.integers(0, 256, loaded.spec.warped_shape, dtype=np.uint8),
                             np.zeros(loaded.spec.packed_nelem, np.float32), loaded.host_inputs)
    result.update(phase='synthetic_inference')
    store(report_path, result)
    timing = benchmark_engine(loaded.engine, prepare, spec['output_shapes'], dtypes, samples=samples)
    entry = cache.entry(spec['sha256'])
    result.update(state='passed', synthetic_inference=True, engine_name=entry.path.name,
                  engine_sha256=digest(entry.path), cuda_graph_active=loaded.engine.graph_exec is not None, **timing)
  except BaseException as error:
    result.update(state='failed', error=f'{type(error).__name__}: {error}'[:2000])
    details = []
    for path in sorted((stage / 'cache/engines').glob('*.build.json'))[:4]:
      record = json.loads(path.read_text())
      details.append({**{key: record.get(key) for key in ('state', 'trt_version', 'device', 'workspace_bytes')},
                      'failure_diagnosis': record.get('failure_diagnosis'),
                      'error': str(record.get('error', ''))[:1000],
                      'warning_error_tail': [str(item)[:600] for item in record.get('warning_error_tail', [])[-8:]]})
    result['builder_diagnostics'] = details
  finally:
    if host is not None:
      try:
        closing_engine = getattr(getattr(host, 'loaded', None), 'engine', None)
        host.close()
        verify_cleanup(closing_engine)
      except Exception as error:
        result.update(state='failed', cleanup_error=f'{type(error).__name__}: {error}'[:1000])
    store(report_path, result)


def publish(stage, destination, result, offroad_check=None):
  """Add a verified plan/sidecar pair without ever replacing an existing artifact."""
  name = result.get('engine_name', '')
  if (not re.fullmatch(r'[a-f0-9]{16}\.[A-Za-z0-9_.-]+\.plan', name)
      or result.get('state') != 'passed' or result.get('synthetic_inference') is not True):
    raise ValueError('Incomplete or invalid candidate evidence')
  plan = stage / 'cache/engines' / name
  meta = plan.with_suffix('.json')
  if plan.is_symlink() or meta.is_symlink() or destination.is_symlink():
    raise ValueError('Engine artifacts/directories cannot be symlinks')
  value = json.loads(meta.read_text())
  if (value.get('spec', {}).get('sha256') != result.get('model_sha256')
      or digest(plan) != result.get('engine_sha256')):
    raise ValueError('Candidate engine checksum or model identity changed')
  destination.mkdir(exist_ok=True)
  target = destination / name
  target_meta = target.with_suffix('.json')
  if target.exists() or target_meta.exists() or target.is_symlink() or target_meta.is_symlink():
    if (not target.is_symlink() and not target_meta.is_symlink() and target.is_file() and target_meta.is_file()
        and digest(target) == result['engine_sha256'] and json.loads(target_meta.read_text()) == value):
      return name  # Validated existing cache, unchanged bytes/settings.
    raise FileExistsError('Existing engine/metadata preserved; inspect it before any replacement')
  # Copy to a private destination-side directory first. The published inode
  # must not share writable forensic/staging files. link() adds without replacement.
  with tempfile.TemporaryDirectory(prefix='.nexo-publish-', dir=destination) as temporary:
    copied_plan, copied_meta = Path(temporary) / 'engine.plan', Path(temporary) / 'engine.json'
    shutil.copyfile(plan, copied_plan)
    shutil.copyfile(meta, copied_meta)
    copied_plan.chmod(0o644)
    copied_meta.chmod(0o644)
    # Latest Carrot builder persists serialized bytes before publishing cache
    # names. Keep our no-clobber publication, including its rollback semantics.
    for path in (copied_plan, copied_meta):
      with path.open('rb') as source:
        os.fsync(source.fileno())
    if digest(copied_plan) != result['engine_sha256']:
      raise ValueError('Plan changed during cache copy')
    if json.loads(copied_meta.read_text()) != value:
      raise ValueError('Metadata changed during cache copy')
    if offroad_check is not None and offroad_check() is not True:
      raise RuntimeError('Offroad state changed before cache publication')
    os.link(copied_plan, target)
    try:
      os.link(copied_meta, target_meta)
      descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
      try:
        os.fsync(descriptor)
      finally:
        os.close(descriptor)
    except BaseException:
      for published, copied in ((target_meta, copied_meta), (target, copied_plan)):
        if published.exists() and published.stat().st_ino == copied.stat().st_ino:
          published.unlink()
      raise

  return name


def prepare(root, offroad_check, timeout=960, samples=100, experiment=False, detach=False, trial=None):
  root = Path(root)
  if type(samples) is not int or not 2 <= samples <= 100:
    raise ValueError('Use 2 to 100 measured synthetic inference samples')
  if type(experiment) is not bool:
    raise ValueError('Experiment selection must be explicit')
  if detach and not experiment:
    raise ValueError('Only isolated experiments may start in the background')
  if trial is not None:
    from openpilot.tools.jetson.engine_experiments import TRIALS
    if not experiment or trial not in TRIALS:
      raise ValueError('Unknown isolated experiment')
  if offroad_check() is not True:
    raise RuntimeError('Actual Comma Offroad state is not verified')
  release = (root / 'current').resolve(strict=True)
  configuration = root / 'updates/nexo-ncm.json'
  original_config = configuration.read_bytes()
  config = json.loads(original_config)
  if config.get('display_only') is not True or config.get('transport') != 'ncm-tcp':
    raise RuntimeError('Existing protected NCM Display Only mode is required; no mode change performed')
  cache = root / 'cache'
  cache.mkdir(exist_ok=True)
  if shutil.disk_usage(cache).free < 4 << 30:
    raise RuntimeError('At least 4 GiB free DATA space is required; no engine build started')
  with (cache / 'nexo-engine-preparation.lock').open('a+') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
      raise RuntimeError('다른 엔진 준비 또는 시험이 실행 중입니다.') from error
    stage = Path(tempfile.mkdtemp(prefix='nexo-experiment-' if experiment else 'nexo-engine-', dir=cache))
    report_path = cache / ('nexo-engine-experiments.json' if experiment else 'nexo-engine-preparation.json')
    report = {'state': 'running', 'stage_directory': str(stage), 'updated': time.monotonic(),
              'vehicle_inference_verified': False, 'driving_activation_performed': False,
              'configuration_changed': False}
    report['experiment_only'] = experiment
    store(report_path, report)
    if detach:
      # Both processes share this open file description. The supervisor keeps
      # the flock after the SSH request returns, including across web restarts.
      if os.fork() != 0:
        return {'state': 'accepted', 'experiment_only': True}
      os.setsid()
      with open(os.devnull, 'r+b', buffering=0) as sink:
        for fd in (0, 1, 2):
          os.dup2(sink.fileno(), fd)
    child = None
    try:
      if detach:
        report.update(supervisor_pid=os.getpid(), boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      supervisor_start=Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
        store(report_path, report)
      selected_worker = worker
      if experiment:
        sys.path.insert(0, str(release))
        from openpilot.tools.jetson.engine_experiments import worker as selected_worker
      args = (root, release, stage, samples) if trial is None else (root, release, stage, samples, trial)
      child = multiprocessing.get_context('spawn').Process(target=selected_worker, args=args)
      child.start()
      deadline = time.monotonic() + timeout
      while child.is_alive():
        if configuration.read_bytes() != original_config:
          raise RuntimeError('Display Only configuration changed during engine preparation')
        if offroad_check() is not True:
          raise RuntimeError('Offroad state lost during engine preparation')
        if time.monotonic() >= deadline:
          raise TimeoutError('Engine preparation exceeded its time budget')
        child.join(timeout=1)
      if (offroad_check() is not True or (root / 'current').resolve() != release
          or configuration.read_bytes() != original_config):
        raise RuntimeError('Offroad state or selected release changed; cache not published')
      report['worker_exitcode'] = child.exitcode
      if child.exitcode is not None and child.exitcode < 0:
        raise RuntimeError(f'Engine worker terminated by signal {-child.exitcode}; inspect kernel logs before attributing it to OOM')
      result = json.loads((stage / 'result.json').read_text())
      if child.exitcode != 0 or result.get('state') != 'passed':
        raise RuntimeError(f"{result.get('phase')}: {result.get('error', 'child inference verification failed')}")
      if digest(release / MODEL) != result.get('model_sha256'):
        raise ValueError('Installed ONNX changed during preparation; original cache preserved')
      if experiment:
        from openpilot.tools.jetson.engine_experiments import TRIALS
        if (result.get('phase') != 'experiment_matrix'
            or [row.get('name') for row in result.get('trials', [])] != ([trial] if trial else list(TRIALS))
            or result.get('production_cache_published') is not False):
          raise ValueError('Incomplete experimental matrix; no production artifact accepted')
        report.update(result, state='experiment_complete', production_cache_published=False)
        return report
      name = publish(stage, cache / 'engines', result,
                     lambda: (offroad_check() is True and (root / 'current').resolve() == release
                              and configuration.read_bytes() == original_config))
      report.update(result, state='engine_ready', engine_name=name)
      return report
    except BaseException as error:
      report.update(state='failed', error=f'{type(error).__name__}: {error}'[:2000])
      if (stage / 'result.json').exists():
        report['candidate'] = json.loads((stage / 'result.json').read_text())
      raise
    finally:
      if child is not None and child.pid and child.is_alive():
        # The worker owns its process group; terminate its CUDA/builder threads too.
        try:
          if os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signal.SIGTERM)
          else:
            child.terminate()
        except ProcessLookupError:
          pass
        child.join(timeout=3)
        if child.is_alive():
          child.kill()
          child.join()
      report['updated'] = time.monotonic()
      store(report_path, report)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--comma-ssh', required=True, help='Existing SSH user@192.168.100.127; no keys or routes are installed')
  parser.add_argument('--samples', type=int, default=100, help='Measured runs after warmup (2..100); no driving activation')
  parser.add_argument('--experiment', action='store_true', help='Explicit FP16/FP32/region comparison; never publishes any plan')
  parser.add_argument('--detach', action='store_true', help='Return after accepting a guarded experiment')
  parser.add_argument('--trial', help='Run only one named comparison in a fresh worker')
  args = parser.parse_args()
  try:
    if not Path('/etc/nv_tegra_release').is_file():
      raise RuntimeError('This command must run on Jetson')
    if args.trial:
      sys.path.insert(0, str((ROOT / 'current').resolve(strict=True)))
    result = prepare(ROOT, lambda: comma_offroad(args.comma_ssh), samples=args.samples, experiment=args.experiment,
                     detach=args.detach, trial=args.trial)
    print(json.dumps(result, indent=2))
    return 0
  except Exception as error:
    print(json.dumps({'state': 'failed', 'error': f'{type(error).__name__}: {error}',
                      'driving_activation_performed': False}))
    return 1


if __name__ == '__main__':
  raise SystemExit(main())
