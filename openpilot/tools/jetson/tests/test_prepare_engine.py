"""Disposable engine preparation; process/GPU calls are simulated, real files are used."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import prepare_engine as prep
from openpilot.tools.jetson import manage

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
    return SimpleNamespace(stdout='{"IsOffroad":"1","IsOnroad":"0","JetlinkEnabled":false}')
  monkeypatch.setattr(prep.subprocess, 'run', run)
  assert prep.comma_offroad('root@192.168.100.127')
  assert calls[0][1]['input'] == prep.OFFROAD_SCRIPT
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


def test_offroad_loss_terminates_in_progress_worker(device, monkeypatch):
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
    prep.prepare(root, lambda: next(answers))
  assert stopped == [(2399999, prep.signal.SIGTERM)]
  assert not (root / 'cache/engines').exists()
  assert (root / 'cache/last-loaded.json').read_bytes() == before[0]


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
  monkeypatch.setattr(prep.subprocess, 'run', lambda *a, **k: SimpleNamespace(
    stdout=json.dumps({'IsOffroad': '1', 'IsOnroad': '0', 'JetlinkEnabled': enabled})))
  assert not prep.comma_offroad('root@192.168.100.127')
