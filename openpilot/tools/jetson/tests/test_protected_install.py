"""Real staged bundles and recovery records with service/GPU I/O simulated explicitly."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import protected_install as installer

OLD = 'a' * 40
NEW = 'b' * 40
MODEL = b'exact NEXO test model'


def test_display_only_install_keeps_carrot_model_and_rollback(device, ncm_device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  config['display_only'] = True
  def probe(release, display_only=False):
    assert display_only and not services[0]
    assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata
  monkeypatch.setattr(installer, 'probe', probe)
  archive = tmp_path / 'bundle.tar.gz'
  installer.install(archive, bundle(archive), config)
  status = json.loads((root / 'updates/nexo-install-status.json').read_text())
  assert status['state'] == 'display_only_installed' and not status['synthetic_inference']
  assert not status['physical_hud_verified'] and not status['vehicle_inference_verified']
  assert helper.load(root)['display_only'] and (root / 'cache/last-loaded.json').read_bytes() == old_metadata
  installer.updater('activate')
  installer.rollback()
  assert selected[0] == previous and services[0] and helper.load(root) is None
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata


def test_display_probe_failure_still_rolls_back(device, ncm_device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  config['display_only'] = True
  def fail(release, display_only=False):
    raise ImportError('HUD dependency unavailable')
  monkeypatch.setattr(installer, 'probe', fail)
  archive = tmp_path / 'bundle.tar.gz'
  with pytest.raises(ImportError, match='HUD dependency'):
    installer.install(archive, bundle(archive), config)
  assert selected[0] == previous and services[0] and helper.load(root) is None
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata


def test_failed_child_reports_phase_and_removes_stale_report(device, monkeypatch):
  root, *_ = device
  report = root / 'cache/nexo-probe.json'
  report.write_text('{"phase":"stale"}')
  class Child:
    pid = 123
    def wait(self, timeout=None):
      if timeout is not None:
        assert not report.exists()
        report.write_text('{"phase":"engine_build_or_load","error":"Conv/GELU tactic failed"}')
      return 1
  monkeypatch.setattr(installer.subprocess, 'Popen', lambda *a, **kw: Child())
  monkeypatch.setattr(installer.os, 'killpg', lambda *a: None, raising=False)
  monkeypatch.setattr(installer.signal, 'SIGKILL', 9, raising=False)
  with pytest.raises(RuntimeError, match='engine_build_or_load: Conv/GELU tactic failed'):
    installer.probe(root)


def marker():
  return {'format': 1, 'source_commit': NEW, 'carrot_commit': 'd0dc96fa32410403dfb2fefff3e2909c11ec109b',
          'runtime': {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'},
          'model': {'sha256': hashlib.sha256(MODEL).hexdigest(), 'nbytes': len(MODEL), 'frame_skip': 4}}


def bundle(path, malicious=None):
  from openpilot.tools.jetson import protected_ncm
  values = {'SOURCE_COMMIT': NEW.encode(), 'NEXO_RUNTIME.json': json.dumps(marker()).encode(),
            'openpilot/tools/jetson/protected_install.py': Path(installer.__file__).read_bytes(),
            'openpilot/tools/jetson/protected_ncm.py': Path(protected_ncm.__file__).read_bytes(),
            'openpilot/selfdrive/modeld/models/driving_supercombo.onnx': MODEL}
  with tarfile.open(path, 'w:gz') as target:
    for name, raw in values.items():
      item = tarfile.TarInfo(name)
      item.size = len(raw)
      target.addfile(item, io.BytesIO(raw))
    if malicious is not None:
      target.addfile(malicious)
  return installer.digest(path)


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/Windows/file', 'folder\\file'])
def test_bundle_rejects_escape_before_any_extraction(tmp_path, name):
  archive = tmp_path / 'bundle.tar.gz'
  bundle(archive, tarfile.TarInfo(name))
  destination = tmp_path / 'stage'
  destination.mkdir()
  with pytest.raises(ValueError, match='Unsafe'):
    installer.extract(archive, destination)
  assert list(destination.iterdir()) == []


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_bundle_rejects_links_and_devices(tmp_path, kind):
  item = tarfile.TarInfo('link')
  item.type, item.linkname = kind, '/etc/passwd'
  archive = tmp_path / 'bundle.tar.gz'
  bundle(archive, item)
  with pytest.raises(ValueError):
    installer.extract(archive, tmp_path / 'stage')


def test_bundle_roundtrip_and_duplicate_rejection(tmp_path):
  archive = tmp_path / 'bundle.tar.gz'
  bundle(archive)
  stage = tmp_path / 'stage'
  installer.extract(archive, stage)
  assert (stage / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx').read_bytes() == MODEL
  bundle(archive, tarfile.TarInfo('SOURCE_COMMIT'))
  with pytest.raises(ValueError):
    installer.extract(archive, tmp_path / 'other')


def test_recovery_refuses_arbitrary_release_before_writing(tmp_path, monkeypatch):
  monkeypatch.setattr(installer, 'ROOT', tmp_path)
  updates = tmp_path / 'updates'
  updates.mkdir()
  record = updates / 'nexo-install-transaction.json'
  record.write_text(json.dumps({'release': '../other', 'last_loaded': base64.b64encode(b'{}').decode()}))
  with pytest.raises(ValueError, match='release name'):
    installer.recover()
  assert record.exists() and not (tmp_path / 'current').exists()


@pytest.fixture
def device(tmp_path, monkeypatch):
  if os.name == 'nt':
    # Windows scanners can transiently hold freshly extracted fixture folders.
    # Keep the real rename and fail after one second; Linux production is unchanged.
    rename, sleep = os.rename, time.sleep
    def fixture_rename(source, destination):
      for attempt in range(11):
        try:
          return rename(source, destination)
        except PermissionError as error:
          if getattr(error, 'winerror', None) != 5 or attempt == 10 or not Path(source).name.startswith('.nexo-stage-'):
            raise
          sleep(.1)
    monkeypatch.setattr(installer.os, 'rename', fixture_rename)
  root = tmp_path / 'runtime'
  previous = root / 'releases' / OLD
  previous.mkdir(parents=True)
  (root / 'cache/models').mkdir(parents=True)
  (root / 'updater').mkdir()
  (root / 'updater/update_host.py').write_text('# original Carrot updater\n')
  old_metadata = json.dumps({'sha256': 'c'*64, 'frame_skip': 4, 'backend': 'trt'}).encode()
  (root / 'cache/last-loaded.json').write_bytes(old_metadata)
  selected, services = [previous], [True]
  monkeypatch.setattr(installer, 'ROOT', root)
  monkeypatch.setattr(installer, 'storage_guard', lambda: None)
  monkeypatch.setattr(installer, 'check_competing_owners', lambda: None)
  monkeypatch.setattr(installer, 'selected_release', lambda: selected[0])
  def switch(name):
    selected[0] = installer.checked_release(name)
  monkeypatch.setattr(installer, 'switch_release', switch)
  monkeypatch.setattr(installer, 'active', lambda: services[0])
  monkeypatch.setattr(installer.os, 'sync', lambda: None, raising=False)
  monkeypatch.setattr(installer.os, 'chown', lambda *a: None, raising=False)
  monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000)))
  def run(*args, **kwargs):
    if args[0] == 'systemctl':
      if args[1] == 'show' and 'update-apply' in args[2]:
        return '/opt/carrot-jetlink/updater/update_host.py activate'
      elif args[1] in ('stop', 'start'):
        services[0] = args[1] == 'start'
      elif 'ExecStart' in args:
        return ('/opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/current/tools/jetlink/'
                + ('hud.py' if 'hud' in args[2] else 'server.py'))
      elif args[1] == 'show':
        return '/opt/carrot-jetlink/updater/update_host.py activate'
      return ''
    return json.dumps([[3, 10], '10.3.0'])
  monkeypatch.setattr(installer, 'run', run)
  monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0 if services[0] else 3))
  monkeypatch.setattr(installer.shutil, 'disk_usage', lambda p: SimpleNamespace(free=10 << 30))
  monkeypatch.setattr(installer.time, 'sleep', lambda n: None)
  return root, previous, selected, services, old_metadata


def test_failed_gpu_probe_restores_previous_model_and_services(device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  archive = tmp_path / 'bundle.tar.gz'
  identity = bundle(archive)
  def fail(release):
    (root / 'cache/last-loaded.json').write_text('{"sha256":"wrong"}')
    raise RuntimeError('GPU build failed')
  monkeypatch.setattr(installer, 'probe', fail)
  with pytest.raises(RuntimeError, match='GPU build failed'):
    installer.install(archive, identity)
  assert selected[0] == previous and services[0]
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata
  assert not (root / 'updates/nexo-install-transaction.json').exists()
  assert json.loads((root / 'updates/nexo-install-status.json').read_text())['state'] == 'rolled_back'


def test_verified_candidate_is_selected_and_physical_proof_is_not_claimed(device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  archive = tmp_path / 'bundle.tar.gz'
  identity = bundle(archive)
  def probe(release):
    assert not services[0] and selected[0] == previous
    (root / 'cache/last-loaded.json').write_text(json.dumps({'sha256': marker()['model']['sha256'], 'frame_skip': 4, 'backend': 'trt'}))
  monkeypatch.setattr(installer, 'probe', probe)
  installer.install(archive, identity)
  assert selected[0].name == 'nexo-' + NEW and services[0]
  status = json.loads((root / 'updates/nexo-install-status.json').read_text())
  assert status['synthetic_inference'] and not status['vehicle_inference_verified'] and not status['physical_hud_verified']
  assert (root / 'updater/update_host.carrot-original.py').read_text() == '# original Carrot updater\n'
  assert (root / 'updater/update_host.py').read_text() == installer.WRAPPER
  installer.install(archive, identity)  # Same committed candidate is idempotent.
  assert (root / 'updates/nexo-install-previous.json').exists()


def test_nexo_channel_does_not_delegate_to_carrot(device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  (previous / 'NEXO_RUNTIME.json').write_text(json.dumps(marker()))
  monkeypatch.setattr(installer.os, 'execv', lambda *a: pytest.fail('Carrot updater was invoked'))
  installer.updater('automatic')


def test_pending_carrot_update_keeps_existing_services_and_updater(device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  (root / 'updates').mkdir()
  (root / 'updates/pending.json').write_text('{}')
  archive = tmp_path / 'bundle.tar.gz'
  with pytest.raises(RuntimeError, match='Carrot update is pending'):
    installer.install(archive, bundle(archive))
  assert selected[0] == previous and services[0]
  assert not (root / 'updater/update_host.carrot-original.py').exists()


def test_service_start_failure_rolls_back_after_candidate_selection(device, tmp_path, monkeypatch):
  root, previous, selected, services, old_metadata = device
  archive = tmp_path / 'bundle.tar.gz'
  def probe(release):
    (root / 'cache/last-loaded.json').write_text(json.dumps({'sha256': marker()['model']['sha256'], 'frame_skip': 4, 'backend': 'trt'}))
  monkeypatch.setattr(installer, 'probe', probe)
  calls = iter([True, False])
  monkeypatch.setattr(installer, 'active', lambda: next(calls))
  with pytest.raises(RuntimeError, match='failed to remain active'):
    installer.install(archive, bundle(archive))
  assert selected[0] == previous and services[0]
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata


def test_boot_guard_recovers_interrupted_install_before_original_updater(device, monkeypatch):
  root, previous, selected, services, old_metadata = device
  (root / 'updates').mkdir()
  installer.install_bridge()
  installer.store(root / 'updates/nexo-install-transaction.json',
                  {'release': previous.name, 'last_loaded': base64.b64encode(old_metadata).decode()})
  candidate = root / 'releases' / ('nexo-' + NEW)
  candidate.mkdir()
  (candidate / 'NEXO_RUNTIME.json').write_text(json.dumps(marker()))
  selected[0], services[0] = candidate, False
  (root / 'cache/last-loaded.json').write_text('{"sha256":"wrong"}')
  delegated = []
  monkeypatch.setattr(installer.os, 'execv', lambda *args: delegated.append(args))
  installer.updater('activate')
  assert selected[0] == previous and not services[0]
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata
  assert delegated and not (root / 'updates/nexo-install-transaction.json').exists()


def test_bad_bundle_checksum_never_stops_services(device, tmp_path):
  root, previous, selected, services, old_metadata = device
  archive = tmp_path / 'bundle.tar.gz'
  bundle(archive)
  with pytest.raises(ValueError, match='checksum differs'):
    installer.install(archive, '0' * 64)
  assert selected[0] == previous and services[0]
  assert not (root / 'updates').exists()


@pytest.fixture
def ncm_device(device, monkeypatch):
  from openpilot.tools.jetson import protected_ncm as ncm
  root, previous, selected, services, old_metadata = device
  actions = []
  helper = SimpleNamespace(__file__=ncm.__file__, validate=ncm.validate, configuration=ncm.configuration, config_path=ncm.config_path,
                           load=lambda target: json.loads(ncm.config_path(target).read_text()) if ncm.config_path(target).exists() else None,
                           discover=lambda name: name, usb_device=lambda name: 'verified-04d8:1234',
                           check_runtime_files=lambda *a: None,
                           activate=lambda target, **kw: actions.append(('activate', kw)),
                           deactivate=lambda target: actions.append(('deactivate', {})),
                           check_listener=lambda: actions.append(('listener', {})))
  monkeypatch.setattr(installer, 'ncm_helper', lambda: helper)
  def probe(release):
    assert not services[0]
    (root / 'cache/last-loaded.json').write_text(json.dumps({'sha256': marker()['model']['sha256'], 'frame_skip': 4, 'backend': 'trt'}))
  monkeypatch.setattr(installer, 'probe', probe)
  return helper, actions, ncm.configuration('enx36770f430ea2', marker()['model']['sha256'])


def test_protected_ncm_install_rollback_and_boot_replay(device, ncm_device, tmp_path):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  archive = tmp_path / 'bundle.tar.gz'
  installer.install(archive, bundle(archive), config)
  assert helper.load(root) == config and selected[0].name == 'nexo-' + NEW
  assert actions == [('activate', {'wait': True}), ('listener', {})]
  installer.updater('automatic')
  assert len(actions) == 2  # Timer never hot-switches/restarts a running host.
  installer.updater('activate')
  assert actions[-1] == ('activate', {})
  installer.rollback()
  assert helper.load(root) is None and actions[-1][0] == 'deactivate'
  assert selected[0] == previous and services[0]
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata


def test_ncm_model_mismatch_never_stops_existing_carrot(device, ncm_device, tmp_path):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  config['model_sha256'] = '0' * 64
  archive = tmp_path / 'bundle.tar.gz'
  with pytest.raises(ValueError, match='explicitly verified comma'):
    installer.install(archive, bundle(archive), config)
  assert selected[0] == previous and services[0] and actions == []
  assert not (root / 'updater/update_host.carrot-original.py').exists()


@pytest.mark.parametrize('stage', ['activate', 'listener'])
def test_ncm_network_or_listener_failure_restores_carrot(device, ncm_device, tmp_path, stage):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  def fail(*a, **kw):
    raise RuntimeError('NCM permission or listener failure')
  setattr(helper, 'check_listener' if stage == 'listener' else 'activate', fail)
  archive = tmp_path / 'bundle.tar.gz'
  with pytest.raises(RuntimeError, match='NCM permission'):
    installer.install(archive, bundle(archive), config)
  assert selected[0] == previous and services[0] and helper.load(root) is None
  assert actions[-1][0] == 'deactivate'
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata


def test_recovery_failure_retains_transaction_and_does_not_claim_success(device, ncm_device, tmp_path):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  def fail(*a, **kw):
    raise RuntimeError('NetworkManager is unavailable')
  helper.activate = helper.deactivate = fail
  archive = tmp_path / 'bundle.tar.gz'
  with pytest.raises(RuntimeError, match='rollback incomplete'):
    installer.install(archive, bundle(archive), config)
  assert (root / 'updates/nexo-install-transaction.json').exists()
  assert json.loads((root / 'updates/nexo-install-status.json').read_text())['state'] != 'installed'


def test_competing_owners_are_refused_without_stopping_yolo(monkeypatch):
  from openpilot.tools.jetson.protected_install import check_competing_owners
  seen = []
  def run(args, **kw):
    seen.append(args)
    return SimpleNamespace(returncode=0 if args[-1] == 'nexo-jetlink-server.service' else 3)
  monkeypatch.setattr(installer.subprocess, 'run', run)
  with pytest.raises(RuntimeError, match='Competing optional owner'):
    check_competing_owners()
  assert not any('nexo-yolo.service' in args or 'stop' in args for args in seen)


def test_ncm_power_loss_record_is_recovered_before_factory_boot(device, ncm_device, monkeypatch):
  root, previous, selected, services, old_metadata = device
  helper, actions, config = ncm_device
  (root / 'updates').mkdir(exist_ok=True)
  installer.install_bridge()
  installer.store(root / 'updates/nexo-install-transaction.json',
                  {'release': previous.name, 'last_loaded': base64.b64encode(old_metadata).decode(), 'ncm_previous': None})
  candidate = root / 'releases' / ('nexo-' + NEW)
  candidate.mkdir()
  (candidate / 'NEXO_RUNTIME.json').write_text(json.dumps(marker()))
  selected[0], services[0] = candidate, False
  installer.store(helper.config_path(root), config)
  (root / 'cache/last-loaded.json').write_text('{"sha256":"wrong"}')
  delegated = []
  monkeypatch.setattr(installer.os, 'execv', lambda *a: delegated.append(a))
  installer.updater('activate')
  assert selected[0] == previous and not services[0] and helper.load(root) is None
  assert actions == [('deactivate', {})] and delegated
  assert (root / 'cache/last-loaded.json').read_bytes() == old_metadata
  assert not (root / 'updates/nexo-install-transaction.json').exists()


@pytest.mark.parametrize('arguments', [
  ['rollback'], ['install'], ['activate', '--ncm-interface', 'usb0'],
  ['install', '--bundle', 'bundle.tar.gz', '--sha256', '0' * 64, '--ncm-interface', 'usb0', '--model-sha256', '0' * 64],
  ['install', '--bundle', 'bundle.tar.gz', '--sha256', '0' * 64, '--ncm-interfac', 'usb0'],
])
def test_cli_typos_and_missing_explicit_confirmation_fail_before_lock_or_storage(monkeypatch, arguments):
  monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace())
  monkeypatch.setattr(sys, 'argv', ['protected_install.py', *arguments])
  monkeypatch.setattr(installer, 'storage_guard', lambda: pytest.fail('CLI error reached storage'))
  with pytest.raises(SystemExit) as result:
    installer.main()
  assert result.value.code == 2
