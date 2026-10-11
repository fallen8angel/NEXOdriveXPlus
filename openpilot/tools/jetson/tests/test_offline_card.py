"""Real small APP files, generated Python hooks and offline process boundaries."""
import ast
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zlib

import pytest

from openpilot.tools.jetson import build_offline_patch as publisher
from openpilot.tools.jetson import build_protected_bundle as exporter
from openpilot.tools.jetson import offline_bootstrap as boot
from openpilot.tools.jetson import offline_card_patch as patcher


def encode(raw):
  return base64.b64encode(raw).decode()


def fixture(tmp_path):
  start, size, offset = 2048, 16384, 4096
  original = b"if __name__ == '__main__':\n  main()\n"
  original += b'\x00' * (6144-len(original))
  changed = b"print('nexo')\n" + b' ' * (6144-14)
  root = bytearray(b'a' * size)
  root[offset-start:offset-start+6144] = original
  alternate = bytearray(root)
  alternate[-1] = ord('b')
  extent = {'offset': offset, 'before': encode(original), 'after': encode(changed),
            'before_sha256': publisher.digest(original), 'after_sha256': publisher.digest(changed)}
  index = {'format': 'carrot-offline-boot-v1', 'setup_bytes': 64 << 20, 'setup_offset': 1024,
           'profiles': [{'root_offset': start, 'root_bytes': size, 'image_bytes': start+size,
                         'release': name, 'root_sha256': publisher.digest(raw),
                         'partition_guard': {'offset': 1024, 'data': encode(b'g' * 512)}, 'patches': [extent]}
                        for raw, name in ((root, 'r2-original'), (alternate, 'r2-sd-nvme-v2'))]}
  card = bytearray(start+size)
  card[1024:1536] = b'g' * 512
  card[start:] = root
  target = tmp_path / 'card.img'
  target.write_bytes(card)
  value = {'format': 'nexo-offline-r2-v1', 'profile': 'r2-original', 'source_commit': 'b'*40,
           'compatibility': index, 'patch': extent}
  return target, value


@pytest.mark.parametrize('position', [0, 1000, 6144])
def test_only_known_allocation_changes_and_interrupted_retry(tmp_path, position):
  target, value = fixture(tmp_path)
  original = target.read_bytes()
  offset, before, after = patcher.validate(value)
  partially_written = bytearray(original)
  partially_written[offset:offset+position] = after[:position]
  target.write_bytes(partially_written)
  result = patcher.apply(target, value, tmp_path / 'backup.json', report=lambda _: None)
  actual = target.read_bytes()
  assert actual[:offset] == original[:offset]
  assert actual[offset+6144:] == original[offset+6144:]
  assert actual[offset:offset+6144] == after
  backup = json.loads((tmp_path / 'backup.json').read_text())
  assert base64.b64decode(backup['before']) == before
  assert result['state'] == 'pc_patch_verified' and result['installation_started'] is False
  patcher.apply(target, value, tmp_path / 'backup.json', report=lambda _: None)
  assert target.read_bytes() == actual


@pytest.mark.parametrize('location', ['root', 'helper', 'guard'])
def test_unknown_changes_refused_without_any_card_write(tmp_path, location):
  target, value = fixture(tmp_path)
  changed = bytearray(target.read_bytes())
  offset = {'root': len(changed)-1, 'helper': value['patch']['offset'], 'guard': 1024}[location]
  changed[offset] = 255
  target.write_bytes(changed)
  with pytest.raises(ValueError):
    patcher.apply(target, value, tmp_path / 'backup.json', report=lambda _: None)
  assert target.read_bytes() == changed


def test_different_recovery_record_blocks_write(tmp_path):
  target, value = fixture(tmp_path)
  original = target.read_bytes()
  backup = tmp_path / 'backup.json'
  backup.write_text('{}')
  with pytest.raises(ValueError, match='recovery file'):
    patcher.apply(target, value, backup, report=lambda _: None)
  assert target.read_bytes() == original


def test_patch_cannot_escape_known_extent(tmp_path):
  _, value = fixture(tmp_path)
  value['patch'] = deepcopy(value['patch'])
  value['patch']['offset'] += 512
  with pytest.raises(ValueError, match='known image-setup'):
    patcher.validate(value)


def test_publisher_binds_exact_bundle_and_preserves_original_entry(tmp_path):
  _, value = fixture(tmp_path)
  archive = tmp_path / 'runtime.tar.gz'
  with tarfile.open(archive, 'w:gz') as bundle:
    for name, raw in {'SOURCE_COMMIT': b'b'*40,
                      'NEXO_RUNTIME.json': json.dumps({'source_commit': 'b'*40,
                        'runtime': {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'}}).encode()}.items():
      item = tarfile.TarInfo(name)
      item.size = len(raw)
      bundle.addfile(item, io.BytesIO(raw))
  hook = Path(publisher.__file__).with_name('offline_boot_hook.py').read_text()
  result = publisher.build(value['compatibility'], archive, hook, tmp_path / 'manifest.json')
  after = base64.b64decode(result['patch']['after'])
  assert len(after) == 6144
  tree = ast.parse(after)
  packed = next(node.args[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'b85decode')
  text = zlib.decompress(base64.b85decode(packed)).decode()
  assert result['bundle_sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
  assert 'NEXO_SHA_PLACEHOLDER' not in text and '--no-block' in text
  assert text.index('  main()\n') < text.index('    nexo_offline_boot()\n')
  assert 'RuntimeMaxSec=1200' in text and 'carrot-jetlink-hud.service' in text
  ast.parse(text, feature_version=(3, 10))


def test_compact_export_keeps_hud_assets_and_exact_model(tmp_path):
  raw = io.BytesIO()
  paths = ['openpilot/selfdrive/assets/fonts/KaiGenGothicKR-Bold.ttf',
           'openpilot/selfdrive/assets/training/unused.png', 'openpilot/system/hardware/tici/updater',
           'openpilot/system/hardware/tici/hardware.py', exporter.MODEL]
  with tarfile.open(fileobj=raw, mode='w') as source:
    for path in paths:
      item = tarfile.TarInfo(path)
      item.size = 5
      source.addfile(item, io.BytesIO(b'exact'))
  archive = tmp_path / 'compact.tar'
  with tarfile.open(archive, 'w') as target:
    exporter.add_archive(target, raw.getvalue(), compact=True)
  with tarfile.open(archive) as target:
    assert set(target.getnames()) == {paths[0], paths[3], paths[4]}
    assert target.extractfile(exporter.MODEL).read() == b'exact'


@pytest.mark.parametrize('good', [True, False])
def test_worker_reports_only_after_installer_exits_and_checks_activation(tmp_path, monkeypatch, good):
  sha, source = 'a'*64, 'b'*40
  folder = tmp_path / 'offline-nexo' / sha
  folder.mkdir(parents=True)
  bundle = folder / 'runtime.tar.gz'
  bundle.write_bytes(b'bundle')
  (tmp_path / 'updates').mkdir()
  module = SimpleNamespace(storage_guard=lambda: None, digest=lambda _: sha, active=lambda: True,
                           selected_release=lambda: Path('nexo-' + source))
  module.store = lambda path, value: path.write_text(json.dumps(value))
  monkeypatch.setattr(boot, 'ROOT', tmp_path)
  monkeypatch.setattr(boot, 'load_installer', lambda _: module)
  # This test isolates systemd/installer lifecycle; archive validation is
  # exercised against real archives separately below.
  monkeypatch.setattr(boot, 'verify_helpers', lambda *args: None)
  results = []
  monkeypatch.setattr(boot, 'export_result', results.append)
  def run(command, **kwargs):
    assert command[:3] == ['/usr/bin/python3', str(folder / 'protected_install.py'), 'install']
    assert not results and not (folder / 'result.json').exists()
    (tmp_path / 'updates/nexo-install-status.json').write_text(json.dumps(
      {'state': 'installed' if good else 'rolled_back', 'source_commit': source, 'synthetic_inference': good}))
    return SimpleNamespace(returncode=0 if good else 1)
  monkeypatch.setattr(boot.subprocess, 'run', run)
  result = boot.worker(bundle, sha, source)
  assert result['state'] == ('installed' if good else 'failed')
  assert result['vehicle_inference_verified'] is False and result['physical_hud_verified'] is False
  assert results == [result] and json.loads((folder / 'result.json').read_text()) == result
  # Completed success/failure never reruns its installer at the next boot.
  monkeypatch.setattr(boot.subprocess, 'run', lambda *a, **kw: pytest.fail('terminal job repeated'))
  assert boot.worker(bundle, sha, source) == result


def test_cached_worker_code_must_match_verified_archive_before_import(tmp_path):
  archive = tmp_path / 'runtime.tar.gz'
  with tarfile.open(archive, 'w:gz') as target:
    for name in ('protected_install.py', 'offline_bootstrap.py'):
      data = b'# original helper\n'
      (tmp_path / name).write_bytes(data)
      item = tarfile.TarInfo('openpilot/tools/jetson/' + name)
      item.size = len(data)
      target.addfile(item, io.BytesIO(data))
  sha = publisher.digest(archive.read_bytes())
  boot.verify_helpers(archive, tmp_path, sha)
  (tmp_path / 'protected_install.py').write_text('raise RuntimeError("altered")')
  with pytest.raises(ValueError, match='Cached offline helper'):
    boot.verify_helpers(archive, tmp_path, sha)


@pytest.mark.parametrize('damaged', [False, True])
def test_boot_hook_preserves_original_services_and_requires_complete_payload(tmp_path, monkeypatch, damaged):
  from openpilot.tools.jetson import offline_boot_hook as hook
  (tmp_path / 'run').mkdir()
  (tmp_path / 'run/carrot-storage.json').write_text('{"state":"protected"}')
  setup = tmp_path / 'setup'
  setup.mkdir()
  archive = setup / 'nexo-offline-runtime.tar.gz'
  with tarfile.open(archive, 'w:gz') as target:
    for name in ('protected_install.py', 'offline_bootstrap.py'):
      item = tarfile.TarInfo('openpilot/tools/jetson/' + name)
      item.size = 4
      target.addfile(item, io.BytesIO(b'pass'))
  raw = archive.read_bytes()
  monkeypatch.setattr(hook, 'NEXO_SHA', publisher.digest(raw))
  monkeypatch.setattr(hook, 'NEXO_BYTES', len(raw))
  from contextlib import contextmanager
  import sys
  @contextmanager
  def setup_mount():
    yield setup / 'identity'
  monkeypatch.setitem(sys.modules, 'protected_storage', SimpleNamespace(setup_mount=setup_mount))
  commands = []
  monkeypatch.setattr(hook.subprocess, 'run', lambda command, **kw: commands.append(command))
  if damaged:
    archive.write_bytes(raw[:-1])
    with pytest.raises(ValueError, match='runtime checksum'):
      hook.nexo_offline_boot(tmp_path)
    assert commands == []
  else:
    hook.nexo_offline_boot(tmp_path)
    assert len(commands) == 1 and commands[0][:2] == ['systemd-run', '--no-block']
    assert '--property=KillMode=control-group' in commands[0]
    assert '--property=RuntimeMaxSec=1200' in commands[0]
    assert (tmp_path / 'opt/carrot-jetlink/offline-nexo' / hook.NEXO_SHA / 'runtime.tar.gz').read_bytes() == raw
