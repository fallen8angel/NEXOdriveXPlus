"""Disposable engine preparation; process/GPU calls are simulated, real files are used."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import prepare_engine as prep
from openpilot.tools.jetson import manage
from openpilot.tools.jetson import ssh_preflight as ssh

SHA = hashlib.sha256(b'native').hexdigest()
NAME = SHA[:16] + '.trt10.3.0.Orin-sm87.plan'


def candidate(stage):
  directory = stage / 'cache/engines'
  directory.mkdir(parents=True)
  plan = directory / NAME
  plan.write_bytes(b'verified-test-plan')
  plan.with_suffix('.json').write_text(json.dumps({'spec': {'sha256': SHA}, 'trt_version': '10.3.0'}))
  return {'state': 'passed', 'phase': 'synthetic_inference', 'synthetic_inference': True,
          'model_sha256': SHA, 'engine_name': NAME, 'engine_sha256': prep.digest(plan)}


def test_publish_validated_pair_only_and_never_replace_existing(tmp_path):
  stage = tmp_path / 'stage'
  result = candidate(stage)
  target = tmp_path / 'engines'
  assert prep.publish(stage, target, result) == NAME
  before = {p.name: p.read_bytes() for p in target.iterdir()}
  assert prep.publish(stage, target, result) == NAME
  assert before == {p.name: p.read_bytes() for p in target.iterdir()}
  (target / NAME).write_bytes(b'old different engine')
  with pytest.raises(FileExistsError, match='preserved'):
    prep.publish(stage, target, result)
  assert (target / NAME).read_bytes() == b'old different engine'


def test_corrupt_plan_and_changed_metadata_are_rejected(tmp_path):
  stage = tmp_path / 'stage'
  result = candidate(stage)
  plan = stage / 'cache/engines' / NAME
  plan.write_bytes(b'corrupt')
  with pytest.raises(ValueError, match='checksum'):
    prep.publish(stage, tmp_path / 'engines', result)
  result['engine_sha256'] = prep.digest(plan)
  plan.with_suffix('.json').write_text(json.dumps({'spec': {'sha256': 'b'*64}}))
  with pytest.raises(ValueError, match='identity'):
    prep.publish(stage, tmp_path / 'engines', result)


def test_partial_publish_cleans_up_only_its_own_plan(tmp_path, monkeypatch):
  stage = tmp_path / 'stage'
  result = candidate(stage)
  target = tmp_path / 'engines'
  link = prep.os.link
  def fail_second(source, destination):
    if Path(destination).suffix == '.json':
      raise OSError('disk full')
    link(source, destination)
  monkeypatch.setattr(prep.os, 'link', fail_second)
  with pytest.raises(OSError, match='disk full'):
    prep.publish(stage, target, result)
  assert not (target / NAME).exists()


@pytest.mark.parametrize('failed_sync', [1, 2, 3])
def test_publication_sync_failure_preserves_old_cache_and_removes_own_pair(tmp_path, monkeypatch, failed_sync):
  stage, target = tmp_path / 'stage', tmp_path / 'engines'
  result = candidate(stage)
  target.mkdir()
  (target / 'old.plan').write_bytes(b'previous model engine')
  calls = []
  original = prep.os.fsync
  def sync(descriptor):
    calls.append(descriptor)
    if len(calls) == failed_sync:
      raise OSError('injected persistence failure')
    original(descriptor)
  monkeypatch.setattr(prep.os, 'fsync', sync)
  with pytest.raises(OSError, match='persistence'):
    prep.publish(stage, target, result)
  assert {path.name: path.read_bytes() for path in target.iterdir()} == {'old.plan': b'previous model engine'}
  assert len(calls) == failed_sync


def test_both_files_are_synced_before_first_public_name(tmp_path, monkeypatch):
  stage, target = tmp_path / 'stage', tmp_path / 'engines'
  result = candidate(stage)
  events = []
  original_sync, original_link = prep.os.fsync, prep.os.link
  def sync(descriptor):
    events.append('sync')
    original_sync(descriptor)
  def link(source, destination):
    events.append('link')
    original_link(source, destination)
  monkeypatch.setattr(prep.os, 'fsync', sync)
  monkeypatch.setattr(prep.os, 'link', link)
  prep.publish(stage, target, result)
  assert events == ['sync', 'sync', 'link', 'link', 'sync']


def test_metadata_change_during_copy_is_not_published(tmp_path, monkeypatch):
  stage, target = tmp_path / 'stage', tmp_path / 'engines'
  result = candidate(stage)
  original = prep.shutil.copyfile
  def copy(source, destination):
    original(source, destination)
    if Path(destination).suffix == '.json':
      Path(destination).write_text('{"spec":{"sha256":"different-model"}}')
  monkeypatch.setattr(prep.shutil, 'copyfile', copy)
  with pytest.raises(ValueError, match='Metadata changed'):
    prep.publish(stage, target, result)
  assert not list(target.iterdir())


@pytest.fixture
def device(tmp_path, monkeypatch):
  root = tmp_path / 'runtime'
  release = root / 'releases/old'
  release.mkdir(parents=True)
  model = release / prep.MODEL
  model.parent.mkdir(parents=True)
  model.write_bytes(b'native')
  (root / 'current').symlink_to(release)
  cache = root / 'cache'
  cache.mkdir()
  (cache / 'last-loaded.json').write_text('previous model')
  updates = root / 'updates'
  updates.mkdir()
  (updates / 'nexo-ncm.json').write_text('{"display_only":true,"transport":"ncm-tcp"}')
  before = (cache / 'last-loaded.json').read_bytes(), (updates / 'nexo-ncm.json').read_bytes()
  monkeypatch.setattr(prep.shutil, 'disk_usage', lambda path: SimpleNamespace(free=8 << 30))
  children = []
  class Child:
    pid = None
    exitcode = 0
    running = False
    def __init__(self, target, args):
      self.stage = args[2]
      children.append(self)
    def start(self):
      result = candidate(self.stage)
      (self.stage / 'result.json').write_text(json.dumps(result))
    def is_alive(self):
      return self.running
    def join(self, timeout=None):
      self.running = False
  monkeypatch.setattr(prep.multiprocessing, 'get_context', lambda mode: SimpleNamespace(Process=Child))
  return root, release, before, children


def test_preparation_keeps_display_config_current_and_last_loaded(device):
  root, release, before, _ = device
  result = prep.prepare(root, lambda: True)
  assert result['state'] == 'engine_ready'
  assert not result['configuration_changed'] and not result['driving_activation_performed']
  assert (root / 'current').resolve() == release
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]
  assert (root / 'updates/nexo-ncm.json').read_bytes() == before[1]


@pytest.mark.parametrize('answer', [False, None, 1])
def test_no_build_without_strict_offroad_proof(device, answer):
  root, _, _, children = device
  with pytest.raises(RuntimeError, match='Offroad'):
    prep.prepare(root, lambda: answer)
  assert not children and not (root / 'cache/engines').exists()


@pytest.mark.parametrize('samples', [True, 0, 1, 101, '100'])
def test_invalid_benchmark_budget_never_starts_worker(device, samples):
  root, _, _, children = device
  with pytest.raises(ValueError, match='samples'):
    prep.prepare(root, lambda: True, samples=samples)
  assert not children


def test_offroad_loss_before_publish_preserves_configuration(device):
  root, _, before, _ = device
  answers = iter([True, False])
  with pytest.raises(RuntimeError, match='changed'):
    prep.prepare(root, lambda: next(answers))
  report = json.loads((root / 'cache/nexo-engine-preparation.json').read_text())
  assert report['state'] == 'failed' and not (root / 'cache/engines').exists()
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]
  assert (root / 'updates/nexo-ncm.json').read_bytes() == before[1]


def test_offroad_ssh_uses_fixed_read_only_command(monkeypatch):
  calls = []
  def run(args, **kwargs):
    calls.append((args, kwargs))
    return SimpleNamespace(stdout='{"IsOffroad":"1","IsOnroad":"0","JetlinkEnabled":false}', returncode=0)
  monkeypatch.setattr(ssh, 'inventory', lambda *a: {})
  monkeypatch.setattr(ssh, 'ncm_check', lambda *a: {'ok': True})
  monkeypatch.setattr(ssh.subprocess, 'run', run)
  assert prep.comma_offroad('root@192.168.100.127')
  assert calls[0][1]['input'] == ssh.OFFROAD_SCRIPT
  for target in ('-oProxyCommand=bad', 'root@10.0.0.1', 'root@192.168.100.127;bad'):
    with pytest.raises(ValueError):
      prep.comma_offroad(target)
  assert len(calls) == 1


def test_inventory_distinguishes_model_identity_from_plan_digest(tmp_path, monkeypatch):
  release = tmp_path / 'current'
  model = release / Path('openpilot/selfdrive/modeld/models/driving_supercombo.onnx')
  model.parent.mkdir(parents=True)
  model.write_bytes(b'ONNX model bytes')
  sha = hashlib.sha256(model.read_bytes()).hexdigest()
  (release / 'NEXO_RUNTIME.json').write_text(json.dumps({'model': {'sha256': sha}}))
  stage = tmp_path / 'stage'
  candidate(stage)
  directory = tmp_path / 'cache/engines'
  directory.mkdir(parents=True)
  plan = directory / (sha[:16] + '.trt10.3.0.Orin-sm87.plan')
  plan.write_bytes(b'compiled engine bytes')
  plan.with_suffix('.json').write_text(json.dumps({'spec': {'sha256': sha}}))
  monkeypatch.setattr(manage, 'run', lambda *a: {'output': 'test runtime'})
  result = manage.installed_model(tmp_path)
  info = result['engine_inventory'][0]
  assert info['matches_installed_onnx'] is True
  assert info['model_sha256'] == sha
  assert info['plan_sha256'] == hashlib.sha256(plan.read_bytes()).hexdigest()
  assert info['plan_sha256'] != sha and not info['loadability_verified']


def test_legacy_build_sidecar_is_classified_without_rewriting_it(tmp_path, monkeypatch):
  engines = tmp_path / 'cache/engines'
  engines.mkdir(parents=True)
  path = engines / 'f73a9e535523d5e9.trt10.3.0.Orin-sm87.build.json'
  message = 'IBuilder::buildSerializedNetwork: Error Code 10: Internal Error ('
  message += 'Could not find any implementation for node vision/node_conv2d_1 + vision/node_gelu_1.)'
  path.write_text(json.dumps({'state': 'failed', 'error': 'TensorRT returned no engine; see the build log',
    'warning_error_tail': [{'severity': 'Severity.ERROR', 'message': message}]}))
  original = path.read_bytes()
  monkeypatch.setattr(manage, 'run', lambda *a: {'output': 'test runtime'})
  result = manage.installed_model(tmp_path)
  diagnosis = result['engine_build_errors'][0]['failure_diagnosis']
  assert diagnosis['category'] == 'conv_gelu_tactic_unavailable'
  assert diagnosis['reported_code_10'] is True and diagnosis['root_cause_proven'] is False
  assert path.read_bytes() == original


@pytest.mark.parametrize('experiment', [False, True])
def test_offroad_loss_terminates_in_progress_worker(device, monkeypatch, experiment):
  root, _, before, _ = device
  stopped = []
  class Child:
    pid = 2399999
    exitcode = None
    running = True
    def __init__(self, **kwargs):
      pass
    def start(self):
      pass
    def is_alive(self):
      return self.running
    def join(self, timeout=None):
      self.running = False
  monkeypatch.setattr(prep.multiprocessing, 'get_context', lambda mode: SimpleNamespace(Process=Child))
  monkeypatch.setattr(prep.os, 'getpgid', lambda pid: pid)
  monkeypatch.setattr(prep.os, 'killpg', lambda pid, sig: stopped.append((pid, sig)))
  answers = iter([True, False])
  with pytest.raises(RuntimeError, match='Offroad state lost'):
    prep.prepare(root, lambda: next(answers), experiment=experiment)
  assert stopped == [(2399999, prep.signal.SIGTERM)]
  assert not (root / 'cache/engines').exists()
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]


def test_experiment_and_preparation_share_one_device_lock(device):
  root, _, _, _ = device
  lock_path = root / 'cache/nexo-engine-preparation.lock'
  with lock_path.open('a+') as held:
    prep.fcntl.flock(held, prep.fcntl.LOCK_EX)
    for experiment in (True, False):
      with pytest.raises(RuntimeError, match='실행 중'):
        prep.prepare(root, lambda: True, experiment=experiment)
    assert not list((root / 'cache').glob('nexo-experiment-*'))


def test_detached_supervisor_survives_request_and_aborts_on_gate_loss(device, tmp_path):
  import os
  import subprocess
  import sys
  import time
  from pathlib import Path
  root, _, _, _ = device
  gate = tmp_path / 'simulated-offroad'
  gate.write_text('1')
  (tmp_path / 'dummy_worker.py').write_text(
    "import os, time\ndef worker(root, release, stage, samples):\n"
    "  os.setsid()\n  (root / 'worker-pid').write_text(str(os.getpid()))\n  time.sleep(30)\n")
  command = [sys.executable, '-c', """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from openpilot.tools.jetson import prepare_engine as prep, engine_experiments as exp
from dummy_worker import worker
exp.worker = worker
prep.shutil.disk_usage = lambda _: SimpleNamespace(free=8 << 30)
root, gate = map(Path, sys.argv[1:])
print(json.dumps(prep.prepare(root, lambda: gate.read_text() == '1', experiment=True, detach=True)))
""", str(root), str(gate)]
  env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(tmp_path), str(Path(prep.__file__).resolve().parents[3]),
                                                     os.environ.get('PYTHONPATH', '')])}
  report = root / 'cache/nexo-engine-experiments.json'
  def wait_until(check):
    deadline = time.monotonic() + 8
    while not check() and time.monotonic() < deadline:
      time.sleep(.05)
    assert check()
  try:
    run = subprocess.run(command, capture_output=True, text=True, env=env, timeout=8, check=True)
    assert json.loads(run.stdout)['state'] == 'accepted'
    wait_until(lambda: (root / 'worker-pid').exists())
    duplicate = subprocess.run(command, capture_output=True, text=True, env=env, timeout=8)
    assert duplicate.returncode != 0 and len(list((root / 'cache').glob('nexo-experiment-*'))) == 1
  finally:
    gate.write_text('0')
  wait_until(lambda: json.loads(report.read_text())['state'] == 'failed')
  assert 'Offroad state lost' in json.loads(report.read_text())['error']
  assert not Path('/proc', (root / 'worker-pid').read_text()).exists()


def test_build_failure_keeps_existing_engine_and_diagnostic_reason(device, monkeypatch):
  root, _, before, _ = device
  engines = root / 'cache/engines'
  engines.mkdir()
  old = engines / 'old.plan'
  old.write_bytes(b'old-engine')
  class Child:
    pid = None
    exitcode = 0
    def __init__(self, target, args):
      self.stage = args[2]
    def start(self):
      (self.stage / 'result.json').write_text(json.dumps({'state': 'failed', 'phase': 'engine_build_or_load',
                                                         'error': 'Conv/GELU tactic failed'}))
    def is_alive(self):
      return False
  monkeypatch.setattr(prep.multiprocessing, 'get_context', lambda mode: SimpleNamespace(Process=Child))
  with pytest.raises(RuntimeError, match='Conv/GELU'):
    prep.prepare(root, lambda: True)
  assert old.read_bytes() == b'old-engine'
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]
  report = json.loads((root / 'cache/nexo-engine-preparation.json').read_text())
  assert 'Conv/GELU tactic failed' in report['error']
  assert report['candidate']['phase'] == 'engine_build_or_load'


def test_publication_rechecks_offroad_after_copy(tmp_path):
  stage = tmp_path / 'stage'
  result = candidate(stage)
  target = tmp_path / 'engines'
  with pytest.raises(RuntimeError, match='Offroad state changed'):
    prep.publish(stage, target, result, lambda: False)
  assert not (target / NAME).exists()


def test_synthetic_test_alone_without_passed_result_cannot_publish(tmp_path):
  stage = tmp_path / 'stage'
  result = candidate(stage)
  result['state'] = 'failed'
  with pytest.raises(ValueError, match='evidence'):
    prep.publish(stage, tmp_path / 'engines', result)


def test_insufficient_data_space_does_not_start_worker(device, monkeypatch):
  root, _, before, children = device
  monkeypatch.setattr(prep.shutil, 'disk_usage', lambda path: SimpleNamespace(free=(4 << 30) - 1))
  with pytest.raises(RuntimeError, match='4 GiB'):
    prep.prepare(root, lambda: True)
  assert not children and (root / 'cache/last-loaded.json').read_bytes() == before[0]


@pytest.mark.parametrize('python_version,architecture,trt', [
  ((3, 12), 'aarch64', '10.3.0'), ((3, 10), 'x86_64', '10.3.0'), ((3, 10), 'aarch64', '11.0.0'),
])
def test_runtime_abi_mismatch_cannot_reach_builder(python_version, architecture, trt):
  marker = {'runtime': {'arch': 'aarch64', 'tensorrt': '10.3.0'}}
  with pytest.raises(ValueError, match='ABI'):
    prep.runtime_preflight(marker, python_version, architecture, trt)
  prep.runtime_preflight(marker, (3, 10), 'aarch64', '10.3.0')


def test_preparation_cannot_run_in_existing_driving_mode(device):
  root, _, _, children = device
  (root / 'updates/nexo-ncm.json').write_text('{"display_only":false,"transport":"ncm-tcp"}')
  with pytest.raises(RuntimeError, match='Display Only'):
    prep.prepare(root, lambda: True)
  assert not children


@pytest.mark.parametrize('enabled', [True, None, 0, 'false'])
def test_offroad_with_enabled_or_unknown_join_marker_is_not_safe(monkeypatch, enabled):
  monkeypatch.setattr(ssh, 'inventory', lambda *a: {})
  monkeypatch.setattr(ssh, 'ncm_check', lambda *a: {'ok': True})
  monkeypatch.setattr(ssh.subprocess, 'run', lambda *a, **k: SimpleNamespace(
    stdout=json.dumps({'IsOffroad': '1', 'IsOnroad': '0', 'JetlinkEnabled': enabled}), returncode=0))
  if enabled is True:
    assert not prep.comma_offroad('root@192.168.100.127')
  else:
    with pytest.raises(ssh.SSHFailure, match='SSH_REMOTE_RESPONSE_INVALID'):
      prep.comma_offroad('root@192.168.100.127')


def test_reverse_ssh_failure_never_starts_worker_or_changes_existing_model(device, monkeypatch):
  root, release, before, children = device
  monkeypatch.setattr(ssh, 'probe', lambda *args: {'ok': False, 'code': 'SSH_AUTH_DENIED',
                                               'recommendation': 'approved key required', 'checked_monotonic': 1.})
  with pytest.raises(ssh.SSHFailure, match='SSH_AUTH_DENIED'):
    prep.prepare(root, ssh.CommaGuard('comma@192.168.100.127', root=root), experiment=True)
  assert children == [] and not list((root / 'cache').glob('nexo-experiment-*'))
  assert (root / 'current').resolve() == release
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]
  assert (root / 'updates/nexo-ncm.json').read_bytes() == before[1]


def test_candidate_validation_uses_existing_supervision_and_never_publishes_engine(device, monkeypatch):
  root, release, before, _ = device
  launched = []
  class Child:
    pid = None
    exitcode = 0
    def __init__(self, target, args):
      self.args = args
      launched.append((target.__module__, args[-1]))
    def start(self):
      prep.store(self.args[2] / 'result.json', {'state': 'passed', 'phase': 'candidate_accuracy',
        'numerical_validation_passed': True, 'source_model_sha256': SHA, 'production_cache_published': False,
        'driving_activation_performed': False, 'vehicle_approved': False})
    def is_alive(self):
      return False
    def join(self, timeout=None):
      pass
  monkeypatch.setattr(prep.multiprocessing, 'get_context', lambda mode: SimpleNamespace(Process=Child))
  monkeypatch.setattr(prep.shutil, 'disk_usage', lambda path: SimpleNamespace(free=512 << 20))
  monkeypatch.setattr(prep, 'publish', lambda *a: (_ for _ in ()).throw(AssertionError('No production publication')))
  result = prep.prepare(root, lambda: True, validation_source='existing-trial', samples=2)
  assert launched == [('openpilot.tools.jetson.engine_candidates', 'existing-trial')]
  assert result['state'] == 'validation_complete' and result['disk_budget']['engine_copy_bytes'] == 0
  stage = Path(result['stage_directory'])
  assert result['validation_result_sha256'] == prep.digest(stage / 'result.json')
  assert (root / 'current').resolve() == release
  assert (root / 'updates/nexo-ncm.json').read_bytes() == before[1]
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]
  assert not (root / 'cache/engines').exists()


def test_candidate_validation_cannot_be_combined_with_build_or_bypass_offroad(device):
  root, _, _, children = device
  with pytest.raises(ValueError, match='cannot combine'):
    prep.prepare(root, lambda: True, experiment=True, validation_source='existing')
  with pytest.raises(RuntimeError, match='Offroad'):
    prep.prepare(root, lambda: False, validation_source='existing')
  assert children == []
