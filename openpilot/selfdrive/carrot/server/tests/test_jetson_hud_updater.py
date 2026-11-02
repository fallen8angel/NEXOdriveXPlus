"""Safety tests for comma-driven Jetson HUD update; never touch a live device."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from openpilot.selfdrive.carrot.server.services import jetson_hud_updater as upd


COMMIT = 'a' * 40


def assets():
  tag = 'jetson-hud-' + COMMIT
  return [{'name': n, 'size': 123, 'browser_download_url':
           f'https://github.com/{upd.REPO}/releases/download/{tag}/{n}'}
          for n in (upd.PACKAGE, upd.CHECKSUM)]


def test_release_must_match_exact_nexo_main_and_target(monkeypatch):
  def fake(url):
    if url.endswith('/branches/NEXO'):
      return {'commit': {'sha': COMMIT}}
    return {'draft': False, 'prerelease': False, 'tag_name': 'jetson-hud-' + COMMIT,
            'target_commitish': COMMIT, 'assets': assets()}
  monkeypatch.setattr(upd, 'json_request', fake)
  assert upd.release_info()['commit'] == COMMIT


@pytest.mark.parametrize('field,value', [
  ('tag_name', 'jetson-hud-' + 'b' * 40),
  ('target_commitish', 'b' * 40),
  ('draft', True),
])
def test_unsafe_or_wrong_release_is_rejected(monkeypatch, field, value):
  def fake(url):
    if url.endswith('/branches/NEXO'):
      return {'commit': {'sha': COMMIT}}
    return {'draft': False, 'prerelease': False, 'tag_name': 'jetson-hud-' + COMMIT,
            'target_commitish': COMMIT, 'assets': assets(), field: value}
  monkeypatch.setattr(upd, 'json_request', fake)
  with pytest.raises(upd.UpdateError):
    upd.release_info()


def test_reject_untrusted_download_origin(monkeypatch):
  bad = assets()
  bad[0]['browser_download_url'] = 'https://evil.example/update.zip'
  def fake(url):
    if url.endswith('/branches/NEXO'):
      return {'commit': {'sha': COMMIT}}
    return {'draft': False, 'prerelease': False, 'tag_name': 'jetson-hud-' + COMMIT,
            'target_commitish': COMMIT, 'assets': bad}
  monkeypatch.setattr(upd, 'json_request', fake)
  with pytest.raises(upd.UpdateError):
    upd.release_info()


def make_zip(source_commit=COMMIT, omit=None, corrupt=False):
  members = {}
  for name in upd.FILES:
    if name not in ('SHA256SUMS', 'nexo-hud-repair.json'):
      members[name] = ('payload for ' + name).encode('utf-8')
  bundle = members['nexo-hud-repair.tar.gz']
  members['nexo-hud-repair.json'] = json.dumps({
    'source_commit': source_commit, 'bundle_sha256': hashlib.sha256(bundle).hexdigest()
  }).encode('utf-8')
  checksums = {name: hashlib.sha256(contents).hexdigest() for name, contents in members.items()}
  members['SHA256SUMS'] = ''.join(f'{digest}  {name}\n' for name, digest in checksums.items()).encode('ascii')
  if corrupt:
    members['nexo-hud-repair.tar.gz'] = b'tampered'
  if omit:
    members.pop(omit)
  output = io.BytesIO()
  with zipfile.ZipFile(output, 'w') as archive:
    for name, contents in members.items():
      archive.writestr(name, contents)
  return output.getvalue()


@pytest.mark.parametrize('invalid', ['commit', 'file', 'missing'])
def test_reject_bad_zip_before_any_ssh(tmp_path, monkeypatch, invalid):
  data = make_zip(source_commit='b' * 40 if invalid == 'commit' else COMMIT,
                  corrupt=invalid == 'file',
                  omit='protected_install.py' if invalid == 'missing' else None)
  def fake_download(url, dest, limit):
    dest.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()
  monkeypatch.setattr(upd, 'download', fake_download)
  monkeypatch.setattr(upd, 'request', lambda url, limit:
                      (hashlib.sha256(data).hexdigest() + '  ' + upd.PACKAGE).encode('ascii'))
  info = {'download': 'url', 'checksum': 'url', 'commit': COMMIT, 'size': len(data)}
  with pytest.raises(upd.UpdateError):
    upd.unpack_verified(tmp_path, info)


def test_valid_zip_is_checked_before_stage(tmp_path, monkeypatch):
  data = make_zip()
  monkeypatch.setattr(upd, 'download', lambda url, dest, limit:
                      (dest.write_bytes(data), hashlib.sha256(data).hexdigest()))
  monkeypatch.setattr(upd, 'request', lambda url, limit:
                      (hashlib.sha256(data).hexdigest() + '  ' + upd.PACKAGE).encode('ascii'))
  folder, digest = upd.unpack_verified(tmp_path, {
    'download': 'url', 'checksum': 'url', 'commit': COMMIT, 'size': len(data)})
  assert set(p.name for p in folder.iterdir()) == set(upd.FILES)
  assert digest == hashlib.sha256((folder / 'nexo-hud-repair.tar.gz').read_bytes()).hexdigest()


def test_preflight_only_no_install_if_parked_unknown(monkeypatch):
  monkeypatch.setattr(upd, 'parked', lambda: False)
  with pytest.raises(upd.UpdateError, match='Offroad'):
    upd.require_parked()


def test_invalid_ssh_target_rejected_before_network(tmp_path, monkeypatch):
  monkeypatch.setattr(upd, 'ROOT', tmp_path)
  monkeypatch.setattr(upd, 'LOCK', tmp_path / 'lock')
  monkeypatch.setattr(upd, 'STATUS', tmp_path / 'status')
  monkeypatch.setattr(upd, 'release_info', lambda: pytest.fail('network must not run'))
  with pytest.raises(upd.UpdateError):
    upd.apply('-oProxyCommand=evil', COMMIT)


def test_remote_operations_always_use_batch_ssh_and_fixed_command(monkeypatch):
  calls = []
  def runner(args, timeout):
    calls.append(args)
    return ''
  monkeypatch.setattr(upd, 'run', runner)
  upd.ssh('jetlink@192.168.100.250', 'true')
  assert calls[0][:2] == ['ssh', '-T']
  assert 'StrictHostKeyChecking=yes' in calls[0]
  assert calls[0][-1] == 'true'
