"""Safety tests for comma-driven Jetson HUD update; never touch a live device."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError
import subprocess
import time
import zipfile

import pytest

from openpilot.selfdrive.carrot.server.services import jetson_hud_updater as upd


COMMIT = 'a' * 40


def assets():
  tag = 'jetson-hud-' + COMMIT
  return [{'name': n, 'size': 123, 'browser_download_url':
           f'https://github.com/{upd.REPO}/releases/download/{tag}/{n}'}
          for n in (upd.PACKAGE, upd.CHECKSUM)]


def test_missing_release_is_not_an_installable_package(monkeypatch):
  def fake(url):
    if url.endswith('/branches/NEXO'):
      return {'commit': {'sha': COMMIT}}
    raise HTTPError(url, 404, 'Not Found', {}, None)
  monkeypatch.setattr(upd, 'json_request', fake)
  with pytest.raises(upd.PackageNotPublished) as caught:
    upd.release_info()
  assert caught.value.commit == COMMIT
  assert '아직 생성되지 않았습니다' in str(caught.value)


def test_github_api_quota_reports_kst_reset_and_specific_failure_code(monkeypatch):
  reset = int(time.time()) + 300
  class Exhausted:
    def open(self, req, timeout):
      raise HTTPError(req.full_url, 403, 'Forbidden',
                      {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(reset)},
                      io.BytesIO(b'{"message":"API rate limit exceeded"}'))
  monkeypatch.setattr(upd, 'OPENER', Exhausted())
  with pytest.raises(upd.GitHubRateLimited) as exc:
    upd.request(upd.API + '/branches/NEXO', 1024)
  assert exc.value.reset_at == reset
  assert 'KST' in str(exc.value)
  assert upd.classify_failure('checking', exc.value)[0] == 'GITHUB_RATE_LIMIT'
  record = {'stage': 'failed', 'failed_stage': 'checking', 'failure_code': 'GITHUB_RATE_LIMIT'}
  assert upd.export_update_diagnostic(record)['manual_retry_possible_after_checks'] is True


def test_known_github_quota_reset_is_shared_and_skips_repeated_rest(monkeypatch, tmp_path):
  monkeypatch.setattr(upd, 'QUOTA_STATE', tmp_path / 'quota.json')
  reset = int(time.time()) + 300
  calls = []
  def exhausted(url, size):
    calls.append(url)
    raise upd.GitHubRateLimited(reset)
  monkeypatch.setattr(upd, 'request', exhausted)
  for _ in range(2):
    with pytest.raises(upd.GitHubRateLimited) as exc:
      upd.json_request(upd.API + '/branches/NEXO')
    assert exc.value.reset_at == reset
  assert calls == [upd.API + '/branches/NEXO']
  assert json.loads(upd.QUOTA_STATE.read_text()) == {'reset_at': reset}


def test_expired_or_malformed_github_quota_cache_never_blocks_rest(monkeypatch, tmp_path):
  monkeypatch.setattr(upd, 'QUOTA_STATE', tmp_path / 'quota.json')
  calls = []
  def valid_response(url, size):
    calls.append(url)
    return b'{"commit":{"sha":"ok"}}'
  monkeypatch.setattr(upd, 'request', valid_response)
  for record in ('not-json', json.dumps({'reset_at': int(time.time()) - 1}),
                 json.dumps({'reset_at': int(time.time()) + 90000})):
    upd.QUOTA_STATE.write_text(record)
    assert upd.json_request(upd.API + '/branches/NEXO') == {'commit': {'sha': 'ok'}}
  assert len(calls) == 3


def test_unknown_quota_reset_does_not_persist_a_permanent_block(monkeypatch, tmp_path):
  monkeypatch.setattr(upd, 'QUOTA_STATE', tmp_path / 'quota.json')
  calls = []
  def no_reset(url, size):
    calls.append(url)
    raise upd.GitHubRateLimited()
  monkeypatch.setattr(upd, 'request', no_reset)
  for _ in range(2):
    with pytest.raises(upd.GitHubRateLimited):
      upd.json_request(upd.API + '/branches/NEXO')
  assert len(calls) == 2
  assert not upd.QUOTA_STATE.exists()


def test_rate_limited_api_uses_only_matching_git_head_tag_and_assets(monkeypatch):
  calls = []
  def quota(url):
    raise upd.GitHubRateLimited(int(time.time()) + 300)
  def git_ref(ref):
    calls.append(ref)
    return COMMIT
  def size(url, limit):
    calls.append(url)
    return 123 if url.endswith(upd.PACKAGE) else 64
  monkeypatch.setattr(upd, 'json_request', quota)
  monkeypatch.setattr(upd, 'git_ref_commit', git_ref)
  monkeypatch.setattr(upd, 'release_asset_size', size)
  info = upd.release_info()
  assert info['commit'] == COMMIT and info['selection'] == 'git_fallback'
  assert calls[:2] == ['refs/heads/NEXO', 'refs/tags/jetson-hud-' + COMMIT]
  assert info['download'].startswith('https://github.com/' + upd.REPO + '/releases/download/')
  assert info['size'] == 123
  assert len(calls) == 4


@pytest.mark.parametrize('tag_commit', [None, 'b' * 40])
def test_rate_limit_fallback_rejects_missing_or_mismatched_tag(monkeypatch, tag_commit):
  monkeypatch.setattr(upd, 'git_ref_commit',
                      lambda ref: COMMIT if ref == 'refs/heads/NEXO' else tag_commit)
  monkeypatch.setattr(upd, 'release_asset_size', lambda *args: pytest.fail('No asset request for bad tag'))
  with pytest.raises(upd.PackageNotPublished if tag_commit is None else upd.UpdateError):
    upd.release_info_without_api()


def test_rate_limit_fallback_rejects_missing_release_asset(monkeypatch):
  monkeypatch.setattr(upd, 'git_ref_commit', lambda ref: COMMIT)
  def missing(url, limit):
    raise HTTPError(url, 404, 'Not Found', {}, None)
  monkeypatch.setattr(upd, 'release_asset_size', missing)
  with pytest.raises(upd.PackageNotPublished):
    upd.release_info_without_api()


def test_rate_limited_release_listing_never_guesses_a_compatible_package(monkeypatch):
  monkeypatch.setattr(upd, 'release_info',
                      lambda: (_ for _ in ()).throw(upd.PackageNotPublished(COMMIT)))
  monkeypatch.setattr(upd, 'stored_rate_limit_reset', lambda: None)
  monkeypatch.setattr(upd, 'json_request',
                      lambda url: (_ for _ in ()).throw(upd.GitHubRateLimited()))
  monkeypatch.setattr(upd, 'git_ref_commit',
                      lambda ref: COMMIT if ref == 'refs/heads/NEXO' else None)
  with pytest.raises(upd.PackageNotPublished):
    upd.select_release_info('1' * 40)


@pytest.mark.parametrize('quota_at', ['listing', 'forward', 'delta'])
def test_quota_during_optional_discovery_recovers_only_exact_git_release(monkeypatch, quota_at):
  installed, candidate, head = '1' * 40, '2' * 40, '3' * 40
  monkeypatch.setattr(upd, 'release_info',
                      lambda: (_ for _ in ()).throw(upd.PackageNotPublished(head)))
  monkeypatch.setattr(upd, 'stored_rate_limit_reset', lambda: None)
  calls = []
  def rest(url):
    calls.append(url)
    if '/releases?' in url:
      stage = 'listing'
      result = [fallback_release_record(installed, '2026-11-18T10:00:00Z'),
                fallback_release_record(candidate, '2026-11-18T11:00:00Z')]
    elif f'/compare/{installed}...{candidate}' in url:
      stage, result = 'forward', {'status': 'ahead', 'behind_by': 0, 'ahead_by': 1}
    elif f'/compare/{candidate}...{head}' in url:
      stage, result = 'delta', {'status': 'ahead', 'behind_by': 0, 'ahead_by': 1, 'files': []}
    else:
      pytest.fail('Unexpected REST request')
    if stage == quota_at:
      raise upd.GitHubRateLimited()
    return result
  monkeypatch.setattr(upd, 'json_request', rest)
  refs = []
  def git_ref(ref):
    refs.append(ref)
    return head
  monkeypatch.setattr(upd, 'git_ref_commit', git_ref)
  monkeypatch.setattr(upd, 'release_asset_size', lambda url, limit: 123)
  info = upd.select_release_info(installed)
  assert info['commit'] == head and info['selection'] == 'git_fallback'
  assert refs == ['refs/heads/NEXO', 'refs/tags/jetson-hud-' + head]
  assert len(calls) == {'listing': 1, 'forward': 2, 'delta': 3}[quota_at]


def test_known_rest_cooldown_skips_optional_listing_after_missing_git_release(monkeypatch):
  monkeypatch.setattr(upd, 'release_info',
                      lambda: (_ for _ in ()).throw(upd.PackageNotPublished(COMMIT)))
  monkeypatch.setattr(upd, 'stored_rate_limit_reset', lambda: int(time.time()) + 300)
  monkeypatch.setattr(upd, 'json_request', lambda url: pytest.fail('REST cooldown must be respected'))
  with pytest.raises(upd.PackageNotPublished):
    upd.select_release_info('1' * 40)


def test_optional_compatibility_discovery_has_bounded_unique_candidates(monkeypatch):
  installed, head = '1' * 40, 'f' * 40
  monkeypatch.setattr(upd, 'release_info',
                      lambda: (_ for _ in ()).throw(upd.PackageNotPublished(head)))
  monkeypatch.setattr(upd, 'stored_rate_limit_reset', lambda: None)
  records = [fallback_release_record(installed, '2026-11-18T20:00:00Z'),
             fallback_release_record(head, '2026-11-18T19:00:00Z')]
  for n in range(2, 9):
    row = fallback_release_record(str(n) * 40, f'2026-11-18T{18-n:02d}:00:00Z')
    records.extend([row, row])
  monkeypatch.setattr(upd, 'json_request', lambda url: records)
  checked = []
  def incompatible(before, candidate, after):
    assert before == installed and after == head
    checked.append(candidate)
    return False
  monkeypatch.setattr(upd, 'compatible_published_release', incompatible)
  with pytest.raises(upd.PackageNotPublished):
    upd.select_release_info(installed)
  assert checked == ['2' * 40, '3' * 40, '4' * 40]


def test_git_ref_discovery_rejects_malformed_or_untrusted_refs(monkeypatch):
  class Reply:
    returncode = 0
    stdout = 'bad-sha\trefs/heads/NEXO\n'
  monkeypatch.setattr(upd.subprocess, 'run', lambda *args, **kwargs: Reply())
  with pytest.raises(upd.UpdateError):
    upd.git_ref_commit('refs/heads/NEXO')
  with pytest.raises(upd.UpdateError):
    upd.git_ref_commit('refs/heads/unsafe')


def test_github_forbidden_without_rate_limit_signal_is_not_mislabeled(monkeypatch):
  class Forbidden:
    def open(self, req, timeout):
      raise HTTPError(req.full_url, 403, 'Forbidden',
                      {'X-RateLimit-Remaining': '59'}, io.BytesIO(b'Forbidden'))
  monkeypatch.setattr(upd, 'OPENER', Forbidden())
  with pytest.raises(HTTPError):
    upd.request(upd.API + '/branches/NEXO', 1024)


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



def fallback_release_record(commit, published):
  tag = 'jetson-hud-' + commit
  return {'tag_name': tag, 'target_commitish': commit, 'draft': False,
          'prerelease': False, 'published_at': published,
          'assets': [{'name': name, 'size': 123,
                      'browser_download_url': f'https://github.com/{upd.REPO}/releases/download/{tag}/{name}'}
                     for name in (upd.PACKAGE, upd.CHECKSUM)]}


def test_fallback_selects_only_newer_verified_compatible_display_release(monkeypatch):
  installed, candidate, head = '1' * 40, '2' * 40, '3' * 40
  def missing():
    raise upd.PackageNotPublished(head)
  monkeypatch.setattr(upd, 'release_info', missing)
  def github(url):
    if url.endswith('/releases?per_page=30'):
      return [fallback_release_record(installed, '2026-11-18T10:00:00Z'),
              fallback_release_record(candidate, '2026-11-18T11:00:00Z')]
    if f'/compare/{installed}...{candidate}' in url:
      return {'status': 'ahead', 'behind_by': 0, 'ahead_by': 1}
    if f'/compare/{candidate}...{head}' in url:
      return {'status': 'ahead', 'behind_by': 0, 'ahead_by': 2,
              'files': [{'filename': 'openpilot/tools/jetson/tests/test_engine_candidates.py'},
                        {'filename': 'openpilot/tools/jetson/engine_candidates.py'}]}
    raise AssertionError(url)
  monkeypatch.setattr(upd, 'json_request', github)
  selected = upd.select_release_info(installed)
  assert selected['commit'] == candidate
  assert selected['selection'] == 'compatible_fallback'
  assert selected['latest_commit'] == head


@pytest.mark.parametrize('changed_path', [
  'openpilot/tools/jetson/hud.py',
  'openpilot/selfdrive/modeld/jetlink/ncm_client.py',
  'openpilot/tools/jetson/model_accuracy.py',
])
def test_fallback_refuses_display_or_unknown_changes(monkeypatch, changed_path):
  installed, candidate, head = '1' * 40, '2' * 40, '3' * 40
  def missing():
    raise upd.PackageNotPublished(head)
  monkeypatch.setattr(upd, 'release_info', missing)
  def github(url):
    if '/releases?' in url:
      return [fallback_release_record(installed, '2026-11-18T10:00:00Z'),
              fallback_release_record(candidate, '2026-11-18T11:00:00Z')]
    if f'/compare/{installed}...{candidate}' in url:
      return {'status': 'ahead', 'behind_by': 0, 'ahead_by': 1}
    return {'status': 'ahead', 'behind_by': 0, 'ahead_by': 1,
            'files': [{'filename': changed_path}]}
  monkeypatch.setattr(upd, 'json_request', github)
  with pytest.raises(upd.PackageNotPublished):
    upd.select_release_info(installed)


def test_fallback_allows_comma_management_only_diff():
  assert upd.non_display_path('openpilot/selfdrive/carrot/server/features/jetson.py')
  assert upd.non_display_path('openpilot/selfdrive/carrot/server/features/jetson_pages.py')
  assert upd.non_display_path('openpilot/selfdrive/carrot/server/services/jetson_hud_updater.py')
  assert upd.non_display_path('tools/nexo_home_offroad_check.sh')
  assert not upd.non_display_path('tools/nexo_home_offroad_check_bypass.sh')
  assert not upd.non_display_path('openpilot/selfdrive/carrot/cluster/cluster.py')
  assert not upd.non_display_path('openpilot/tools/jetson/hud.py')


def test_fallback_never_reinstalls_or_downgrades_installed_baseline(monkeypatch):
  installed, head = '1' * 40, '3' * 40
  monkeypatch.setattr(upd, 'release_info', lambda: (_ for _ in ()).throw(upd.PackageNotPublished(head)))
  def github(url):
    if '/releases?' in url:
      return [fallback_release_record(installed, '2026-11-18T10:00:00Z'),
              fallback_release_record('2' * 40, '2026-11-18T09:00:00Z')]
    if f'/compare/{installed}...' in url:
      return {'status': 'behind', 'behind_by': 1, 'ahead_by': 0}
    raise AssertionError(url)
  monkeypatch.setattr(upd, 'json_request', github)
  with pytest.raises(upd.PackageNotPublished):
    upd.select_release_info(installed)


def test_fallback_uses_git_ancestry_not_out_of_order_release_timestamps(monkeypatch):
  installed, candidate, head = '1' * 40, '2' * 40, '3' * 40
  monkeypatch.setattr(upd, 'release_info', lambda: (_ for _ in ()).throw(upd.PackageNotPublished(head)))
  def github(url):
    if url.endswith('/releases?per_page=30'):
      # Candidate is newer in Git history but its CI finished before the
      # ancestor's slow CI, so published_at is earlier.
      return [fallback_release_record(installed, '2026-11-19T01:03:35Z'),
              fallback_release_record(candidate, '2026-11-19T01:02:05Z')]
    if f'/compare/{installed}...{candidate}' in url:
      return {'status': 'ahead', 'behind_by': 0, 'ahead_by': 6}
    if f'/compare/{candidate}...{head}' in url:
      return {'status': 'ahead', 'behind_by': 0, 'ahead_by': 3,
              'files': [{'filename': 'openpilot/selfdrive/carrot/server/features/jetson.py'}]}
    raise AssertionError(url)
  monkeypatch.setattr(upd, 'json_request', github)
  assert upd.select_release_info(installed)['commit'] == candidate


def test_fallback_requires_a_verified_installed_commit(monkeypatch):
  monkeypatch.setattr(upd, 'release_info', lambda: (_ for _ in ()).throw(upd.PackageNotPublished('3' * 40)))
  monkeypatch.setattr(upd, 'json_request', lambda url: pytest.fail('No GitHub releases query without SSH baseline'))
  with pytest.raises(upd.PackageNotPublished):
    upd.select_release_info(None)

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
  monkeypatch.setattr(upd, 'ssh', lambda target, command, timeout: COMMIT)
  monkeypatch.setattr(upd, 'select_release_info', lambda installed: {'commit': COMMIT, 'size': 123})
  def corrupt(work, info, progress):
    progress(40)
    status = json.loads(upd.STATUS.read_text())
    assert status['received_bytes'] == 40 and status['total_bytes'] == 123
    raise upd.UpdateError('checksum mismatch')
  monkeypatch.setattr(upd, 'unpack_verified', corrupt)
  progress = []
  with pytest.raises(upd.UpdateError, match='checksum'):
    upd.apply('jetlink@jetson', COMMIT, on_progress=progress.append)
  assert progress == ['HUD checking', 'HUD downloading', 'HUD downloading 40/123 B', 'HUD failed']
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


def test_observer_matches_only_this_workers_lock_refusal(monkeypatch):
  attempt = '1' * 32
  previous = '2' * 32
  status = json.dumps({'state': 'display_only_installed', 'source_commit': COMMIT})
  receipt = status + '\nSOURCE_COMMIT=' + COMMIT + '\nUNIT=inactive\nBUSY_ATTEMPT='
  outputs = iter((receipt + previous, receipt + attempt))
  monkeypatch.setattr(upd, 'ssh', lambda *args, **kwargs: next(outputs))
  monkeypatch.setattr(upd.time, 'sleep', lambda _: None)
  with pytest.raises(upd.UpdateLockBusy) as stopped:
    upd.observe_install('jetlink@jetson', COMMIT, baseline_status=status, attempt=attempt)
  assert upd.classify_failure('verifying', stopped.value)[0] == 'UPDATE_LOCK_BUSY'
  assert upd.classify_failure('transferring', upd.UpdateError('NEXO_UPDATE_LOCK_BUSY action=storage'))[0] == 'UPDATE_LOCK_BUSY'
  assert upd.classify_failure('transferring', upd.UpdateError('other resource temporarily unavailable'))[0] == 'JETSON_LINK'
  record = {'stage': 'failed', 'commit': COMMIT, 'failure_code': 'UPDATE_LOCK_BUSY'}
  for installed, retry in ((COMMIT, False), ('a' * 40 if COMMIT != 'a' * 40 else 'b' * 40, True)):
    value = upd.export_update_diagnostic(record, installed_commit=installed)
    assert value['stage'] == 'failed'
    assert value['manual_retry_possible_after_checks'] is retry
