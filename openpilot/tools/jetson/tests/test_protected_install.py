"""Real staged bundles and recovery records with service/GPU I/O simulated explicitly."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import protected_install as installer

OLD = 'a' * 40
NEW = 'b' * 40
MODEL = b'exact NEXO test model'


def marker():
  return {'format': 1, 'source_commit': NEW, 'carrot_commit': 'd0dc96fa32410403dfb2fefff3e2909c11ec109b',
          'runtime': {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'},
          'model': {'sha256': hashlib.sha256(MODEL).hexdigest(), 'nbytes': len(MODEL), 'frame_skip': 4}}


def bundle(path, malicious=None):
  values = {'SOURCE_COMMIT': NEW.encode(), 'NEXO_RUNTIME.json': json.dumps(marker()).encode(),
            'openpilot/tools/jetson/protected_install.py': Path(installer.__file__).read_bytes(),
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
