"""Use real files, links and rollback records; never contact hardware or stop services."""
import json
import os
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import hud_storage as storage
from openpilot.selfdrive.carrot.server.services import jetson_hud_updater as updater

CURRENT, PREVIOUS, INCOMING = 'a' * 40, 'b' * 40, 'c' * 40


def package(root, commit, stamp=100):
  path = root / 'updates' / ('hud-' + commit[:12])
  path.mkdir()
  (path / 'nexo-hud-repair.tar.gz').write_bytes(b'old package' * 1000)
  (path / 'nexo-hud-repair.json').write_text(json.dumps({'source_commit': commit}))
  for item in [*path.iterdir(), path]:
    os.utime(item, (stamp, stamp))
  return path


@pytest.fixture
def device(tmp_path):
  (tmp_path / 'updates').mkdir()
  (tmp_path / 'releases' / ('nexo-' + CURRENT)).mkdir(parents=True)
  (tmp_path / 'current').symlink_to('releases/nexo-' + CURRENT)
  (tmp_path / 'updates/nexo-install-previous.json').write_text(json.dumps({'release': 'nexo-' + PREVIOUS}))
  for number in range(1, 4):
    package(tmp_path, str(number) * 40, 100 + number)
  return tmp_path


def test_preserves_current_rollback_incoming_and_two_latest(device):
  for commit in (CURRENT, PREVIOUS, INCOMING):
    package(device, commit, 50)
  assert [row['folder'] for row in storage.inventory(device, INCOMING)] == ['hud-' + '1' * 12]


@pytest.mark.parametrize('kind', ['plan', 'onnx', 'result', 'directory', 'symlink', 'hardlink', 'corrupt', 'mismatch', 'recent'])
def test_unknown_ai_and_unsafe_packages_are_never_selected(device, kind):
  path = device / 'updates' / ('hud-' + '1' * 12)
  if kind in ('plan', 'onnx', 'result'):
    (path / ('ai.' + kind)).write_bytes(b'keep')
  elif kind == 'directory':
    (path / 'experiments').mkdir()
  elif kind == 'symlink':
    (path / 'protected_install.py').symlink_to('/etc/passwd')
  elif kind == 'hardlink':
    os.link(path / 'nexo-hud-repair.tar.gz', device / 'ai.plan')
  elif kind == 'corrupt':
    (path / 'nexo-hud-repair.json').write_text('broken')
  elif kind == 'mismatch':
    (path / 'nexo-hud-repair.json').write_text(json.dumps({'source_commit': CURRENT}))
  else:
    os.utime(path, None)
  if kind != 'recent':
    for item in [*path.iterdir(), path]:
      os.utime(item, (50, 50), follow_symlinks=False)
  assert path.name not in {item['folder'] for item in storage.inventory(device, INCOMING)}


@pytest.mark.parametrize('name', ['pending.json', 'transaction.json', 'nexo-install-transaction.json'])
def test_pending_transactions_block_cleanup(device, name):
  (device / 'updates' / name).write_text('{}')
  with pytest.raises(RuntimeError, match='recovery'):
    storage.reclaim(device, INCOMING, 100, apply=True)
  assert len(list((device / 'updates').glob('hud-*'))) == 3


def test_corrupt_rollback_state_blocks_cleanup(device):
  (device / 'updates/nexo-install-previous.json').write_text('corrupt')
  with pytest.raises(ValueError):
    storage.reclaim(device, INCOMING, 100, apply=True)


def test_unresolved_status_blocks_cleanup(device):
  (device / 'updates/nexo-install-status.json').write_text('{"state":"rollback_failed"}')
  with pytest.raises(RuntimeError, match='Unresolved'):
    storage.reclaim(device, INCOMING, 100, apply=True)


def test_dry_run_and_actual_allocated_bytes(device, monkeypatch):
  path = device / 'updates' / ('hud-' + '1' * 12)
  allocated = sum(p.stat().st_blocks * 512 for p in path.iterdir())
  required = storage.MIN_FREE + 100 + (64 << 20)
  before = required - allocated
  monkeypatch.setattr(storage.shutil, 'disk_usage', lambda root: SimpleNamespace(free=before if path.exists() else required))
  result = storage.reclaim(device, INCOMING, 100)
  assert path.exists() and not result['ready'] and result['reclaimable_bytes'] == allocated
  # Results, engines and live releases remain byte-for-byte untouched.
  engine = device / 'cache/engines/success.plan'
  engine.parent.mkdir(parents=True)
  engine.write_bytes(b'keep engine')
  experiment = device / 'nexo-ai-validation/result.json'
  experiment.parent.mkdir()
  experiment.write_bytes(b'keep result')
  result = storage.reclaim(device, INCOMING, 100, apply=True)
  assert result['ready'] and result['deleted'] == [path.name] and not path.exists()
  assert result['reclaimed_bytes'] == allocated
  assert engine.read_bytes() == b'keep engine' and experiment.read_bytes() == b'keep result'
  assert (device / 'current').resolve().is_dir()


def test_no_deletion_when_space_already_sufficient_or_cannot_be_recovered(device, monkeypatch):
  for free in (10 << 30, 0):
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda root, available=free: SimpleNamespace(free=available))
    result = storage.reclaim(device, INCOMING, 100, apply=True)
    assert result['deleted'] == []
  assert len(list((device / 'updates').glob('hud-*'))) == 3


def test_storage_failure_prevents_transfer_install_and_service_changes(device, monkeypatch):
  calls = []
  for name, value in [('ROOT', device), ('LOCK', device / 'lock'), ('STATUS', device / 'status')]:
    monkeypatch.setattr(updater, name, value)
  monkeypatch.setattr(updater, 'require_parked', lambda: calls.append('parked'))
  monkeypatch.setattr(updater, 'release_info', lambda: {'commit': INCOMING})
  monkeypatch.setattr(updater, 'unpack_verified', lambda *args: (device, 'd' * 64))
  monkeypatch.setattr(updater, 'ssh', lambda *args, **kwargs: '')
  monkeypatch.setattr(updater, 'remote_check', lambda *args: None)
  def insufficient(*args):
    assert calls[-1] == 'parked'
    raise updater.UpdateError('DATA_SPACE: insufficient free DATA')
  monkeypatch.setattr(updater, 'remote_storage', insufficient)
  for name in ('scp_files', 'remote_preflight', 'remote_install'):
    monkeypatch.setattr(updater, name, lambda *args: pytest.fail('No transfer/install on space failure'))
  with pytest.raises(updater.UpdateError, match='DATA_SPACE'):
    updater.apply('jetlink@192.168.100.250', INCOMING)
  assert json.loads(updater.STATUS.read_text())['failure_code'] == 'DATA_SPACE'


def test_small_helper_runs_before_large_upload_without_changing_ssh_verification(tmp_path, monkeypatch):
  for name in updater.FILES:
    (tmp_path / name).write_bytes(b'package')
  commands = []
  def ssh(target, command, timeout):
    commands.append(command)
    return '{"ready":true,"reclaimed_bytes":4096}'
  monkeypatch.setattr(updater, 'ssh', ssh)
  assert updater.remote_storage('jetlink@192.168.100.250', tmp_path, INCOMING)['reclaimed_bytes'] == 4096
  assert commands[0].startswith(f'sudo -n python3 - {INCOMING} 56 --apply')
  assert 'flock' in commands[0] and "guard['storage_guard']()" in commands[0]
