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
  monkeypatch.setattr(upd, 'installation_ready', lambda: (False, 'P단이 아닙니다.'))
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


def test_remote_installer_uses_explicit_parked_confirmation(monkeypatch):
  recorded = []
  monkeypatch.setattr(upd, 'ssh', lambda target, command, timeout: recorded.append(command))
  upd.remote_install('jetlink@192.168.100.250', '/opt/carrot-jetlink/updates/hud-test', 'a' * 64)
  assert len(recorded) == 1
  assert '--confirmed-parked' in recorded[0]
  assert '--confirmed-offroad' not in recorded[0]


def test_download_failure_retains_phase_and_does_not_transfer(tmp_path, monkeypatch):
  for name, path in [('ROOT', tmp_path), ('LOCK', tmp_path/'lock'), ('STATUS', tmp_path/'status')]:
    monkeypatch.setattr(upd, name, path)
  monkeypatch.setattr(upd, 'require_parked', lambda: None)
  monkeypatch.setattr(upd, 'release_info', lambda: {'commit': COMMIT, 'size': 123})
  def corrupt(work, info, progress):
    progress(40)
    status = json.loads(upd.STATUS.read_text())
    assert status['received_bytes'] == 40 and status['total_bytes'] == 123
    raise upd.UpdateError('checksum mismatch')
  monkeypatch.setattr(upd, 'unpack_verified', corrupt)
  with pytest.raises(upd.UpdateError, match='checksum'):
    upd.apply('jetlink@jetson', COMMIT)
  assert json.loads(upd.STATUS.read_text())['failed_stage'] == 'downloading'

def test_comma_github_unreachable_is_not_mistaken_for_jetson_wifi(monkeypatch):
  from urllib.error import URLError
  def fail(url):
    raise URLError('offline')
  monkeypatch.setattr(upd, 'json_request', fail)
  with pytest.raises(upd.UpdateError, match='콤마4.*GitHub'):
    upd.release_info()



def test_update_failure_codes_are_stage_specific_and_do_not_retry_install():
  assert upd.classify_failure('downloading', upd.UpdateError('checksum mismatch'))[0] == 'PACKAGE_INTEGRITY'
  auth = upd.UpdateError('젯슨 SSH 연결이 확인되지 않았습니다.')
  auth.__cause__ = upd.UpdateError('Permission denied (publickey)')
  assert upd.classify_failure('checking_jetson_ssh', auth)[0] == 'SSH_AUTH'
  assert upd.classify_failure('verifying', upd.UpdateError('timeout'))[2] is False


def test_diagnostic_exports_only_known_failure_fields():
  raw = {'stage': 'failed', 'failed_stage': 'verifying',
         'failure_code': 'REMOTE_INSTALL_OR_VERIFY',
         'retry_safe': True, 'advice': 'malicious advice',
         'detail': 'password=my-secret token=private',
         'ssh_target': 'jetlink@192.168.100.250',
         'commit': 'a' * 40, 'updated': 1790000000}
  output = upd.export_update_diagnostic(raw, now=1790000012)
  assert output['failure_code'] == 'REMOTE_INSTALL_OR_VERIFY'
  assert output['manual_retry_possible_after_checks'] is False
  assert output['record_age_s'] == 12
  serialized = json.dumps(output, ensure_ascii=False) + upd.format_update_diagnostic(output)
  assert 'my-secret' not in serialized
  assert '192.168.100.250' not in serialized
  assert 'malicious advice' not in serialized


def test_diagnostic_reports_missing_and_old_uncategorized_failures():
  missing = upd.export_update_diagnostic({'stage': 'idle'})
  assert missing['has_last_attempt'] is False
  assert missing['failure_code'] is None
  old = upd.export_update_diagnostic({'stage': 'failed', 'detail': 'old error'})
  assert old['failure_code'] == 'LEGACY_UNCLASSIFIED'
  assert old['manual_retry_possible_after_checks'] is False
  unknown = upd.export_update_diagnostic({'stage': 'unrecognized'})
  assert unknown['stage'] == 'unknown'


def test_observer_ignores_previous_rollback_while_new_worker_activates(monkeypatch):
  baseline = json.dumps({'state': 'rolled_back', 'release': 'nexo-previous'})
  outputs = iter((
    baseline + '\nSOURCE_COMMIT=' + 'b' * 40 + '\nUNIT=activating',
    json.dumps({'state': 'testing'}) + '\nSOURCE_COMMIT=' + 'b' * 40 + '\nUNIT=activating',
    json.dumps({'state': 'display_only_installed', 'source_commit': COMMIT})
      + '\nSOURCE_COMMIT=' + COMMIT + '\nUNIT=inactive',
  ))
  monkeypatch.setattr(upd, 'ssh', lambda target, command, timeout: next(outputs))
  monkeypatch.setattr(upd.time, 'sleep', lambda _: None)
  assert '설치 완료' in upd.observe_install('jetlink@jetson', COMMIT, baseline_status=baseline)


def test_observer_reports_new_rollback_after_changed_worker_status(monkeypatch):
  baseline = json.dumps({'state': 'rolled_back', 'release': 'nexo-previous'})
  outputs = iter((
    baseline + '\nSOURCE_COMMIT=' + 'b' * 40 + '\nUNIT=activating',
    json.dumps({'state': 'testing'}) + '\nSOURCE_COMMIT=' + 'b' * 40 + '\nUNIT=activating',
    json.dumps({'state': 'rolled_back', 'release': 'nexo-previous', 'error': 'new build failed'})
      + '\nSOURCE_COMMIT=' + 'b' * 40 + '\nUNIT=failed',
  ))
  monkeypatch.setattr(upd, 'ssh', lambda target, command, timeout: next(outputs))
  monkeypatch.setattr(upd.time, 'sleep', lambda _: None)
  with pytest.raises(upd.UpdateError, match='롤백 보고'):
    upd.observe_install('jetlink@jetson', COMMIT, baseline_status=baseline)
