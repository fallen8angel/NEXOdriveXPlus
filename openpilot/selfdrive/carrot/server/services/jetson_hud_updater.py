"""Parked-only protected Jetson HUD updater driven by comma port 7000.

No vehicle-control writes, APP/SETUP changes, unsigned scripts, or unpinned update URLs.
The existing protected installer owns rollback and service restoration.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import zipfile

from .hud_update_safety import installation_ready

REPO = 'fallen8angel/NEXOdriveXPlus'
API = 'https://api.github.com/repos/' + REPO
PACKAGE = 'NEXO-Jetson-HUD-repair.zip'
CHECKSUM = PACKAGE + '.sha256'
ROOT = Path('/data/carrot/jetson-hud-updater')
STATUS = ROOT / 'status.json'
LOCK = ROOT / 'update.lock'
REMOTE_BASE = '/opt/carrot-jetlink/updates'
MAX_ZIP = 400 * 1024 * 1024
MAX_TAR = 600 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{40}\Z')
SSH_TARGET = re.compile(r'(?:[a-z_][a-z0-9_-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.:-]{0,126}\Z')
FILES = ('nexo-hud-repair.tar.gz', 'nexo-hud-repair.json', 'protected_install.py',
         'protected_ncm.py', 'install_hud_update.py', 'SHA256SUMS',
         'Connect-Jetson.ps1', 'INSTALL_KO.md')


class UpdateError(RuntimeError):
  pass


class PinnedRedirect(HTTPRedirectHandler):
  def redirect_request(self, req, fp, code, msg, headers, newurl):
    host = (urlsplit(newurl).hostname or '').lower()
    if urlsplit(newurl).scheme != 'https' or host not in (
      'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'api.github.com'
    ):
      raise UpdateError('허용되지 않은 다운로드 경로로 이동했습니다.')
    return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = build_opener(PinnedRedirect)


def request(url, max_bytes):
  req = Request(url, headers={'Accept': 'application/vnd.github+json',
                              'User-Agent': 'NEXO-HUD-Protected-Updater/1'})
  with OPENER.open(req, timeout=15) as response:
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
      raise UpdateError('GitHub 응답 크기 제한 초과')
    return data


def json_request(url):
  return json.loads(request(url, 1024 * 1024))


def release_info():
  """Only the current NEXO HEAD and its exact, non-draft release are eligible."""
  try:
    # The protected bundle downloads on Comma4. Jetson Wi-Fi is not required
    # for this step, but Comma4 must reach GitHub over its own verified route.
    head = json_request(API + '/branches/NEXO')['commit']['sha']
  except (URLError, TimeoutError) as error:
    raise UpdateError('콤마4에서 GitHub 접속에 실패했습니다. 젯슨 Wi-Fi가 아니라 콤마4 인터넷 연결을 확인해 주세요.') from error
  if not SHA.fullmatch(head):
    raise UpdateError('GitHub 메인 브랜치 버전 검증 실패')
  tag = 'jetson-hud-' + head
  try:
    rel = json_request(API + '/releases/tags/' + tag)
  except HTTPError as error:
    if error.code == 404:
      raise UpdateError('현재 메인 버전의 젯슨 패키지가 아직 생성되지 않았습니다. GitHub HUD 워크플로 완료 후 다시 확인하세요.') from error
    raise
  if rel.get('draft') or rel.get('prerelease') or rel.get('tag_name') != tag:
    raise UpdateError('보호형 HUD 릴리스 식별자가 올바르지 않습니다.')
  if rel.get('target_commitish') != head:
    raise UpdateError('릴리스 대상 커밋이 NEXO 메인과 다릅니다.')
  assets = {asset.get('name'): asset for asset in rel.get('assets', [])}
  for name in (PACKAGE, CHECKSUM):
    asset = assets.get(name)
    expected = f'https://github.com/{REPO}/releases/download/{tag}/{name}'
    if not asset or asset.get('browser_download_url') != expected or not isinstance(asset.get('size'), int):
      raise UpdateError('젯슨 패키지 또는 체크섬이 누락되었거나 예상한 저장소가 아닙니다.')
    if asset['size'] <= 0 or asset['size'] > (MAX_ZIP if name == PACKAGE else 4096):
      raise UpdateError('릴리스 파일 크기가 허용 범위를 벗어났습니다.')
  return {'commit': head, 'tag': tag,
          'download': assets[PACKAGE]['browser_download_url'],
          'checksum': assets[CHECKSUM]['browser_download_url'],
          'size': assets[PACKAGE]['size'],
          'release_url': rel.get('html_url', '')}


def atomic_status(stage, **extra):
  ROOT.mkdir(parents=True, exist_ok=True)
  data = {'stage': stage, 'pid': os.getpid(), 'updated': int(time.time()), **extra}
  tmp = ROOT / ('status-' + str(os.getpid()) + '.tmp')
  tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
  os.replace(tmp, STATUS)


def parked():
  """Legacy boolean compatibility: Offroad or fresh real-vehicle P proof."""
  return installation_ready()[0]


def require_parked():
  # Re-evaluate at each transaction boundary instead of trusting the page.
  approved, detail = installation_ready()
  if not approved:
    raise UpdateError('콤마4가 Offroad이거나 시동 ON/P단 완전 정지 상태여야 합니다. ' + detail)


def run(argv, timeout=30):
  result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False, text=True, errors='replace')
  output = (result.stdout + '\n' + result.stderr)[-4500:]
  if result.returncode:
    raise UpdateError(f'원격 작업 실패({result.returncode}): {output[-700:]}')
  return result.stdout


def ssh(target, command, timeout=30):
  return run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
              '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
              '-o', 'ServerAliveCountMax=3', '--', target, command], timeout)


def download(url, dest, limit, progress=None):
  req = Request(url, headers={'User-Agent': 'NEXO-HUD-Protected-Updater/1'})
  total = 0
  hasher = hashlib.sha256()
  next_report = 0.
  with OPENER.open(req, timeout=30) as response, dest.open('wb') as output:
    while chunk := response.read(512 * 1024):
      total += len(chunk)
      if total > limit:
        raise UpdateError('릴리스 다운로드 용량 제한 초과')
      output.write(chunk)
      hasher.update(chunk)
      if progress is not None and time.monotonic() >= next_report:
        progress(total)
        next_report = time.monotonic() + 1
  return total, hasher.hexdigest()


def unpack_verified(work, info, progress=None):
  zip_path = work / PACKAGE
  args = (info['download'], zip_path, MAX_ZIP)
  total, digest = download(*args) if progress is None else download(*args, progress=progress)
  if total != info['size']:
    raise UpdateError('GitHub 릴리스 파일 크기가 일치하지 않습니다.')
  line = request(info['checksum'], 4096).decode('ascii').strip()
  match = re.fullmatch(r'([0-9a-f]{64})  ' + re.escape(PACKAGE), line)
  if not match or match.group(1) != digest:
    raise UpdateError('릴리스 ZIP SHA256 불일치')
  folder = work / 'package'
  folder.mkdir()
  with zipfile.ZipFile(zip_path) as archive:
    names = archive.namelist()
    if len(names) != len(FILES) or set(names) != set(FILES):
      raise UpdateError('업데이트 ZIP 구성 파일이 예상과 다릅니다.')
    for name in FILES:
      member = archive.getinfo(name)
      cap = MAX_TAR if name == 'nexo-hud-repair.tar.gz' else 1024 * 1024
      if member.is_dir() or member.file_size <= 0 or member.file_size > cap or (member.external_attr >> 16) & 0o170000 == 0o120000:
        raise UpdateError('ZIP 내부 파일 종류 또는 크기 오류: ' + name)
      with archive.open(member) as src, (folder / name).open('wb') as dest:
        shutil.copyfileobj(src, dest, 1024 * 1024)
  entries = (folder / 'SHA256SUMS').read_text(encoding='ascii').splitlines()
  expected = {}
  for line in entries:
    match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
    if not match or match.group(2) in expected:
      raise UpdateError('내부 SHA256 목록 형식 오류')
    expected[match.group(2)] = match.group(1)
  if set(expected) != set(FILES) - {'SHA256SUMS'}:
    raise UpdateError('내부 SHA256 목록 누락')
  for name, checksum in expected.items():
    hasher = hashlib.sha256()
    with (folder / name).open('rb') as f:
      while block := f.read(1024 * 1024):
        hasher.update(block)
    if hasher.hexdigest() != checksum:
      raise UpdateError('내부 파일 SHA256 불일치: ' + name)
  report = json.loads((folder / 'nexo-hud-repair.json').read_text(encoding='utf-8'))
  tar_digest = expected['nexo-hud-repair.tar.gz']
  if report.get('source_commit') != info['commit'] or report.get('bundle_sha256') != tar_digest:
    raise UpdateError('패키지 소스 커밋 또는 보호형 번들 해시 불일치')
  return folder, tar_digest


def remote_check(target, remote):
  script = (
    'test -f /etc/nv_tegra_release && '
    'test -f /run/carrot-storage.json && '
    'mountpoint -q /opt/carrot-jetlink && '
    'test -f /opt/carrot-jetlink/updates/nexo-ncm.json && '
    'sudo -n install -d -o jetlink -g jetlink -m 0750 ' + remote
  )
  ssh(target, script, timeout=30)


def scp_files(target, folder, remote):
  cmd = ['scp', '-q', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
         '-o', 'ConnectTimeout=8', '--']
  cmd.extend(str(folder / name) for name in FILES)
  cmd.append(target + ':' + remote + '/')
  run(cmd, timeout=300)


def remote_preflight(target, remote, digest):
  command = ('cd ' + remote + ' && sha256sum -c SHA256SUMS && '
             'sudo -n python3 install_hud_update.py --bundle nexo-hud-repair.tar.gz '
             '--sha256 ' + digest + ' --check-only')
  result = ssh(target, command, timeout=140)
  if 'NEXO_PREFLIGHT_OK' not in result:
    raise UpdateError('젯슨 보호형 설치 사전 검사 통과 표시가 없습니다.')


def remote_install(target, remote, digest):
  args = ('sudo -n systemd-run --unit=nexo-hud-repair --collect --no-block '
          '--property=Type=oneshot --property=TimeoutStartSec=1200 '
          '--property=KillMode=control-group /usr/bin/python3 '
          + remote + '/install_hud_update.py --bundle '
          + remote + '/nexo-hud-repair.tar.gz --sha256 ' + digest + ' --confirmed-parked')
  ssh(target, args, timeout=30)


def observe_install(target, commit, limit=900):
  # The Jetson's protected installer has its own transaction and rollback.
  # A detached systemd-run can survive a temporary SSH interruption.
  deadline = time.monotonic() + limit
  previous = ''
  while time.monotonic() < deadline:
    try:
      output = ssh(target, 'cat /opt/carrot-jetlink/updates/nexo-install-status.json; '
                          'printf "\\nSOURCE_COMMIT="; cat /opt/carrot-jetlink/current/SOURCE_COMMIT; '
                          'printf "\\nUNIT="; systemctl is-active nexo-hud-repair.service || true', timeout=20)
      status_text = output.split('\nSOURCE_COMMIT=', 1)[0].strip()
      try:
        status = json.loads(status_text)
      except (ValueError, TypeError):
        status = {}
      if (status.get('state') == 'display_only_installed'
          and status.get('source_commit') == commit
          and 'SOURCE_COMMIT=' + commit in output):
        return '보호형 설치 완료 · 콤마/실제 HUD 화면은 별도 확인 필요'
      if any(marker in output for marker in ('rollback_failed', 'rolled_back', 'restore_failed')):
        raise UpdateError('젯슨 설치 실패 또는 롤백 보고: ' + output[-700:])
      previous = output[-300:]
    except UpdateError as error:
      if '롤백 보고' in str(error):
        raise
      previous = str(error)[-200:]
    time.sleep(5)
  raise UpdateError('젯슨 설치 완료 확인 시간 초과. 재설치하지 말고 서비스와 설치 이력을 확인하세요. ' + previous)


def apply(target, commit):
  if not SSH_TARGET.fullmatch(target) or not SHA.fullmatch(commit):
    raise UpdateError('SSH 대상 또는 커밋 형식이 올바르지 않습니다.')
  ROOT.mkdir(parents=True, exist_ok=True)
  with LOCK.open('a+b') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
      raise UpdateError('다른 HUD 업데이트 작업이 실행 중입니다.') from error
    atomic_status('checking', commit=commit)
    try:
      require_parked()
      info = release_info()
      if info['commit'] != commit:
        raise UpdateError('업데이트 확인 후 메인 버전이 변경되었습니다. 다시 확인해 주세요.')
      with tempfile.TemporaryDirectory(prefix='nexo-hud-', dir=ROOT) as temporary:
        atomic_status('downloading', commit=commit)
        def progress(count):
          atomic_status('downloading', commit=commit, received_bytes=count, total_bytes=info['size'])
        folder, digest = unpack_verified(Path(temporary), info, progress)
        require_parked()
        remote = REMOTE_BASE + '/hud-' + commit[:12]
        # Verify the USB/NCM SSH management path before starting a potentially
        # long transfer. A reachable USB carrier alone is not SSH proof.
        atomic_status('checking_jetson_ssh', commit=commit)
        try:
          ssh(target, 'true', timeout=20)
        except UpdateError as error:
          raise UpdateError('젯슨 SSH 연결이 확인되지 않았습니다. USB-NCM 연결 및 인증 상태를 점검해 주세요.') from error
        require_parked()
        atomic_status('transferring', commit=commit)
        remote_check(target, remote)
        scp_files(target, folder, remote)
        require_parked()
        atomic_status('preflight', commit=commit)
        remote_preflight(target, remote, digest)
        require_parked()
        atomic_status('installing', commit=commit, detail='젯슨 독립 설치 작업 실행')
        remote_install(target, remote, digest)
        atomic_status('verifying', commit=commit, detail='젯슨 설치 완료 및 복구 확인 중')
        detail = observe_install(target, commit)
      atomic_status('completed', commit=commit, detail=detail)
    except Exception as error:
      try:
        previous = json.loads(STATUS.read_text(encoding='utf-8'))
      except (OSError, ValueError):
        previous = {}
      failed_stage = previous.get('stage') if previous.get('commit') == commit else None
      code, advice, retry_safe = classify_failure(failed_stage, error)
      atomic_status('failed', commit=commit, failed_stage=failed_stage,
                    failure_code=code, advice=advice, retry_safe=retry_safe,
                    detail=(type(error).__name__ + ': ' + str(error))[-700:])
      raise



def classify_failure(stage, error):
  """Non-secret, actionable update failure codes. No automatic retry."""
  chain = []
  current = error
  for _ in range(3):
    if current is None:
      break
    chain.append(str(current).lower())
    current = current.__cause__
  message = ' '.join(chain)
  if any(s in message for s in ('offroad', 'p단', '완전 정지')):
    return 'PARKING_SAFETY', '정지 및 P단 확인 상태를 확인해 주세요.', False
  if any(s in message for s in ('checksum', 'sha256', 'digest', '해시', '서명')):
    return 'PACKAGE_INTEGRITY', '무결성 확인 실패입니다. 해당 릴리스를 재검증해 주세요.', False
  if '아직 생성되지' in message or ('404' in message and stage == 'checking'):
    return 'PACKAGE_NOT_READY', 'HUD 릴리스 생성 상태를 확인해 주세요.', True
  if 'permission denied' in message or 'publickey' in message or '인증' in message:
    return 'SSH_AUTH', '콤마4에서 젯슨의 SSH 공개키 인증을 확인해 주세요.', True
  if stage in ('checking_jetson_ssh', 'transferring') or any(
      s in message for s in ('connection refused', 'connection timed out', 'no route to host')):
    return 'JETSON_LINK', '젯슨 전원과 USB-NCM 및 SSH 연결을 확인해 주세요.', True
  if stage in ('checking', 'downloading'):
    return 'GITHUB_OR_DOWNLOAD', '콤마4 인터넷 연결과 GitHub 릴리스를 점검해 주세요.', True
  if '롤백' in message or 'rollback' in message:
    return 'INSTALL_ROLLBACK', '설치 이력을 확인하고 자동 재시도하지 마세요.', False
  if stage in ('preflight', 'installing', 'verifying'):
    return 'REMOTE_INSTALL_OR_VERIFY', '설치 결과를 확인하기 전에는 재설치하지 마세요.', False
  return 'UNKNOWN_FAILURE', '실패 단계 및 원격 로그를 확인해 주세요.', False



# Export ONLY known non-secret fields. Raw update errors may contain SSH
# destinations or command output and must never enter the diagnostic ZIP.
UPDATE_DIAGNOSTIC_STAGES = frozenset((
  'idle', 'checking', 'downloading', 'checking_jetson_ssh', 'transferring',
  'preflight', 'installing', 'verifying', 'completed', 'failed', 'unknown',
))
UPDATE_FAILURE_GUIDANCE = {
  'PARKING_SAFETY': ('차량이 완전히 정지하고 P단이 확인되는지 점검해 주세요.', False),
  'PACKAGE_INTEGRITY': ('패키지 SHA256 및 GitHub 릴리스 출처를 다시 확인해 주세요.', False),
  'PACKAGE_NOT_READY': ('메인 커밋에 해당하는 젯슨 패키지 생성 상태를 확인해 주세요.', True),
  'SSH_AUTH': ('콤마4에서 젯슨 SSH 공개키 인증을 확인해 주세요.', True),
  'JETSON_LINK': ('젯슨 전원과 USB-NCM 및 SSH 통신 상태를 확인해 주세요.', True),
  'GITHUB_OR_DOWNLOAD': ('콤마4의 인터넷 연결과 GitHub 릴리스 다운로드 상태를 확인해 주세요.', True),
  'INSTALL_ROLLBACK': ('젯슨 설치 및 롤백 이력을 먼저 검토하고 재설치하지 마세요.', False),
  'REMOTE_INSTALL_OR_VERIFY': ('젯슨 설치 완료 여부를 먼저 확인한 후 다음 조치를 결정하세요.', False),
  'UNKNOWN_FAILURE': ('실패 단계와 업데이트 로그를 확인해 주세요.', False),
  'LEGACY_UNCLASSIFIED': ('예전 업데이트 기록에는 원인 코드가 없습니다. 작업 로그를 확인해 주세요.', False),
}


def export_update_diagnostic(status, *, now=None):
  """Read-only and credential-free summary of the LAST Comma-driven Jetson HUD update.

  This does not inspect or change the remote Jetson. Historic attempts are not
  archived; the status file describes only the most recently recorded attempt.
  """
  data = status if isinstance(status, dict) else {}
  stage = data.get('stage')
  stage = stage if isinstance(stage, str) and stage in UPDATE_DIAGNOSTIC_STAGES else 'unknown'
  recorded = stage != 'idle' and bool(data)
  failed_stage = data.get('failed_stage')
  failed_stage = failed_stage if isinstance(failed_stage, str) and failed_stage in UPDATE_DIAGNOSTIC_STAGES else None
  code = data.get('failure_code')
  code = code if stage == 'failed' and isinstance(code, str) and code in UPDATE_FAILURE_GUIDANCE else None
  if stage == 'failed' and code is None:
    code = 'LEGACY_UNCLASSIFIED'
  guidance, retry_eligible = UPDATE_FAILURE_GUIDANCE[code] if code else (None, None)
  stamp = data.get('updated')
  stamp = stamp if type(stamp) is int and stamp > 0 else None
  current = time.time() if now is None else now
  age = current - stamp if stamp is not None and type(current) in (int, float) else None
  age = round(age, 1) if age is not None and 0 <= age < 366 * 24 * 3600 else None
  commit = data.get('commit')
  commit = commit if isinstance(commit, str) and SHA.fullmatch(commit) else None
  return {'schema_version': 1, 'read_only': True,
          'source': 'comma4_local_jetson_hud_updater_status',
          'has_last_attempt': recorded,
          'stage': stage if recorded else 'idle',
          'last_recorded_at_unix': stamp if recorded else None,
          'record_age_s': age if recorded else None,
          'target_commit': commit if recorded else None,
          'failed_stage': failed_stage if stage == 'failed' else None,
          'failure_code': code,
          'guidance': guidance, 'manual_retry_possible_after_checks': retry_eligible,
          'status_note': (
             '마지막 업데이트 작업 기록이 없습니다.' if not recorded else
             '업데이트 기록의 상태를 확인할 수 없습니다. 설치 성공을 의미하지 않습니다.' if stage == 'unknown' else
             '마지막 작업이 완료되었다고 기록되어 있습니다. 젯슨 실제 버전은 별도 확인이 필요합니다.' if stage == 'completed' else
             '마지막 작업 실패 기록입니다. 자동 재시도는 수행하지 않습니다.' if stage == 'failed' else
             '마지막 기록은 진행 단계이며 현재 실제 실행 여부는 별도로 확인해야 합니다.'),
          'privacy_note': 'SSH 주소, 비밀키, 원본 오류 문구 및 명령 출력은 ZIP에 넣지 않습니다.',
          'limitations': '콤마4의 최근 1회 HUD 업데이트 기록만 확인합니다. 젯슨 전체 설치 이력과 별개입니다.'}


def format_update_diagnostic(value):
  """Concise human-readable companion for the unified diagnostic ZIP."""
  return '\n'.join((
    '엑스플러스 · 젯슨 업데이트 원인별 진단 (콤마4 읽기 전용)',
    '최근 업데이트 기록: ' + ('있음' if value.get('has_last_attempt') else '없음'),
    '마지막 상태: ' + str(value.get('stage') or 'unknown'),
    '실패 단계: ' + str(value.get('failed_stage') or '해당 없음'),
    '실패 코드: ' + str(value.get('failure_code') or '해당 없음'),
    '권장 점검: ' + str(value.get('guidance') or '별도 점검 안내 없음'),
    '수동 재시도 검토 가능: ' + str(value.get('manual_retry_possible_after_checks')),
    '대상 커밋: ' + str(value.get('target_commit') or '확인 불가'),
    '기록 경과 초: ' + str(value.get('record_age_s')),
    '상태 안내: ' + str(value.get('status_note')),
    '수집 범위: ' + str(value.get('limitations')),
    '보안 안내: ' + str(value.get('privacy_note')),
  )) + '\n'


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--target', required=True)
  parser.add_argument('--commit', required=True)
  args = parser.parse_args()
  apply(args.target, args.commit)


if __name__ == '__main__':
  main()
