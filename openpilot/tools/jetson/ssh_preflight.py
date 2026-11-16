"""Read-only, strict-host-key SSH diagnostics. Never provisions or reads private keys."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import time

ROOT = Path('/opt/carrot-jetlink')
COMMA = '192.168.100.127'
JETSON = '192.168.100.250'
TARGET = re.compile(r'[a-zA-Z_][a-zA-Z0-9_-]{0,31}@(192\.168\.100\.(?:127|250))')
OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'PreferredAuthentications=publickey', '-o', 'PasswordAuthentication=no',
           '-o', 'KbdInteractiveAuthentication=no',
           '-o', 'ConnectTimeout=3', '-o', 'ConnectionAttempts=1',
           '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=1']

GUIDANCE = {
  'SSH_AUTH_DENIED': ('해당 방향의 SSH 계정·기존 개인키와 대상 장치의 승인 공개키 지문을 확인하세요. ' +
                      '반대 방향의 성공은 이 인증을 보장하지 않습니다.'),
  'SSH_NO_IDENTITY': ('젯슨 실행 계정에 읽을 수 있는 기본 개인키나 agent가 없습니다. ' +
                      '기존 승인 키의 경로 또는 기존 SSH agent를 설정하세요. 키를 자동 생성하거나 교체하지 않습니다.'),
  'SSH_HOST_KEY_CHANGED': '호스트 키가 저장된 값과 다릅니다. 장치 신원과 새 지문을 직접 대조하세요. known_hosts 삭제나 검증 우회를 자동 수행하지 않습니다.',
  'SSH_HOST_KEY_UNKNOWN': '이 실행 계정의 known_hosts에 신뢰한 대상 키가 없습니다. 기존 승인 파일·SSH 설정과 실제 장치 지문을 대조하세요.',
  'SSH_KEY_UNREADABLE': '선택한 기존 개인키의 경로·소유자·읽기 권한·파일 권한을 확인하세요. 키를 자동 생성·교체·복사하지 않습니다.',
  'SSH_KEY_INVALID': 'SSH가 선택한 키를 읽지 못했습니다. 기존 키 형식과 암호/agent 사용 여부를 확인하세요.',
  'SSH_CONFIG_INVALID': '기존 SSH 설정을 확인하세요. 고정 USB 대상의 사용자·주소와 proxy 설정을 점검하세요.',
  'SSH_CONNECTION_REFUSED': '대상 SSH 서비스의 포트와 실행 상태를 확인하세요. 이 결과만으로 인증 실패라고 판단하지 않습니다.',
  'SSH_NETWORK_UNREACHABLE': 'USB-NCM 주소·직접 경로·carrier와 대상 장치 전원을 확인하세요.',
  'SSH_TIMEOUT': 'USB-NCM 경로·대상 서비스·응답 지연을 확인하세요. 인증 성공은 아직 확인되지 않았습니다.',
  'USB_NCM_UNAVAILABLE': '검증된 USB-NCM 직접 경로가 없습니다. 기존 인터페이스·주소·carrier를 확인하세요. 경로를 자동 변경하지 않습니다.',
  'SSH_UNAVAILABLE': '현재 실행 계정에서 OpenSSH 클라이언트를 실행할 수 있는지 확인하세요.',
  'SSH_REMOTE_RESPONSE_INVALID': 'SSH 원격 명령의 JSON 응답과 Params 읽기 권한을 확인하세요. Offroad로 추정하지 않습니다.',
  'SSH_REMOTE_COMMAND_FAILED': '접속 또는 원격 상태 확인이 실패했습니다. 종료 코드만으로 인증 실패나 Offroad를 확정하지 않습니다.',
}


def classify_failure(stderr='', returncode=None, *, direction='jetson_to_comma', timed_out=False):
  text = str(stderr).lower()
  if timed_out or 'timed out' in text or 'timeout' in text:
    code = 'SSH_TIMEOUT'
  elif 'remote host identification has changed' in text or 'offending ' in text:
    code = 'SSH_HOST_KEY_CHANGED'
  elif 'host key verification failed' in text or 'no ' in text and 'host key is known' in text:
    code = 'SSH_HOST_KEY_UNKNOWN'
  elif 'unprotected private key' in text or 'bad permissions' in text or 'identity file' in text and 'not accessible' in text:
    code = 'SSH_KEY_UNREADABLE'
  elif 'invalid format' in text or 'error in libcrypto' in text or 'incorrect passphrase' in text:
    code = 'SSH_KEY_INVALID'
  elif 'permission denied' in text or 'too many authentication failures' in text:
    code = 'SSH_AUTH_DENIED'
  elif 'connection refused' in text:
    code = 'SSH_CONNECTION_REFUSED'
  elif any(v in text for v in ('no route to host', 'network is unreachable', 'connection reset', 'connection closed')):
    code = 'SSH_NETWORK_UNREACHABLE'
  elif 'bad configuration' in text or 'bad owner or permissions' in text or 'bad configuration option' in text:
    code = 'SSH_CONFIG_INVALID'
  else:
    code = 'SSH_REMOTE_COMMAND_FAILED'
  # Deliberately do not return raw stderr: keys, config commands or banners
  # may contain secrets. Fixed reasons and fingerprints are enough to act on.
  return {'ok': False, 'code': code, 'direction': direction, 'returncode': returncode,
          'recommendation': GUIDANCE[code], 'host_key_checking': 'strict', 'settings_changed': False}


class SSHFailure(RuntimeError):
  def __init__(self, result):
    self.diagnosis = result
    super().__init__(result['code'] + ': ' + result['recommendation'])


def fingerprints(text):
  result = []
  for line in text.splitlines()[:128]:
    parts = line.split()
    for i, part in enumerate(parts[:-1]):
      if part.startswith(('ssh-', 'ecdsa-', 'sk-')):
        try:
          blob = base64.b64decode(parts[i + 1], validate=True)
          result.append('SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('='))
        except ValueError:
          pass
        break
  return sorted(set(result))[:16]


def file_summary(path, public=False):
  path = Path(path).expanduser()
  value = {'path': str(path), 'exists': False}
  try:
    info = path.stat()
    value.update(exists=True, regular=stat.S_ISREG(info.st_mode), readable=os.access(path, os.R_OK),
                 mode=oct(stat.S_IMODE(info.st_mode)), owner_uid=info.st_uid)
    if public and value['regular'] and info.st_size < 65536:
      value['fingerprints'] = fingerprints(path.read_text(errors='replace'))
  except OSError:
    pass
  return value


def identity_path(value):
  if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./@:+-]{1,239}', value) or '..' in Path(value).parts:
    raise ValueError('기존 개인키의 절대 경로만 지정하세요.')
  return value


def ssh_args(target, identity_file=None):
  if not isinstance(target, str) or not TARGET.fullmatch(target):
    raise ValueError('Only the existing USB peer SSH user@192.168.100.127/.250 is supported')
  args = ['ssh', *OPTIONS]
  if identity_file:
    args += ['-o', 'IdentitiesOnly=yes', '-i', identity_path(identity_file)]
  return [*args, '--', target]


def inventory(target, identity_file=None):
  args = ssh_args(target, identity_file)
  try:
    configured = subprocess.run([*args[:-2], '-G', *args[-2:]], capture_output=True, text=True, timeout=4, check=False)
  except (OSError, subprocess.TimeoutExpired) as error:
    raise SSHFailure({'ok': False, 'code': 'SSH_UNAVAILABLE', 'recommendation': GUIDANCE['SSH_UNAVAILABLE'],
                      'settings_changed': False}) from error
  if configured.returncode:
    raise SSHFailure(classify_failure(configured.stderr, configured.returncode))
  fields = {}
  for line in configured.stdout.splitlines():
    key, _, value = line.partition(' ')
    fields.setdefault(key.lower(), []).append(value.strip())
  expected_user, expected_host = target.split('@')
  if (fields.get('hostname', [expected_host])[0] != expected_host
      or fields.get('user', [expected_user])[0] != expected_user
      or fields.get('proxycommand', ['none'])[0] != 'none' or fields.get('proxyjump', ['none'])[0] != 'none'):
    raise SSHFailure({'ok': False, 'code': 'SSH_CONFIG_INVALID', 'recommendation': GUIDANCE['SSH_CONFIG_INVALID'],
                      'settings_changed': False})
  paths = [identity_file] if identity_file else fields.get('identityfile', [])[:12]
  identities = []
  for value in paths:
    value = value.replace('%d', str(Path.home())).replace('%h', expected_host).replace('%r', expected_user)
    item = file_summary(value)
    item['public_key'] = file_summary(str(Path(value).expanduser()) + '.pub', public=True)
    identities.append(item)
  if identity_file:
    item = identities[0]
    if not item['exists'] or not item.get('regular') or not item.get('readable') or int(item['mode'], 8) & 0o077:
      raise SSHFailure({'ok': False, 'code': 'SSH_KEY_UNREADABLE', 'recommendation': GUIDANCE['SSH_KEY_UNREADABLE'],
                        'selected_identity': item, 'settings_changed': False})
  known = []
  for value in fields.get('userknownhostsfile', []):
    for path in shlex.split(value)[:4]:
      known.append(file_summary(path.replace('%d', str(Path.home())).replace('%h', expected_host)))
  return {'target': target, 'client_uid': os.geteuid(), 'client_home': str(Path.home()),
          'identity_files': identities, 'known_hosts': known[:8],
          'agent_available': bool(os.environ.get('SSH_AUTH_SOCK')), 'private_key_contents_collected': False,
          'host_key_checking': 'strict', 'settings_changed': False}


def ncm_check(target):
  destination = target.split('@')[1]
  source = JETSON if destination == COMMA else COMMA
  # Read-only route evidence distinguishes a changing USB device name from
  # an absent source address or a Wi-Fi route. Never repair routes here.
  route_observed = {}
  try:
    row = json.loads(subprocess.run(['ip', '-j', '-4', 'route', 'get', destination, 'from', source],
                                   capture_output=True, text=True, timeout=2, check=True).stdout)
    if len(row) == 1 and isinstance(row[0], dict):
      route_observed = {key: value for key in ('dev', 'src', 'prefsrc', 'gateway')
                        if isinstance((value := row[0].get(key)), str)
                        and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,64}', value)}
    if len(row) != 1 or row[0].get('gateway'):
      raise ValueError('ROUTE_NOT_DIRECT')
    iface = row[0]['dev']
    if not re.fullmatch('[A-Za-z0-9_.:-]{1,15}', iface):
      raise ValueError('INTERFACE_INVALID')
    base = Path('/sys/class/net') / iface
    if (base / 'carrier').read_text().strip() != '1':
      raise ValueError('CARRIER_DOWN')
    if destination == COMMA and (base / 'device/driver').resolve().name != 'cdc_ncm':
      raise ValueError('NOT_CDC_NCM')
    # ip route get ... from SOURCE can omit src/prefsrc on a direct USB route.
    # Verify SOURCE is truly assigned to that same NCM interface instead of
    # rejecting an otherwise valid enx* device name, or trusting Wi-Fi.
    route_source = row[0].get('prefsrc') or row[0].get('src')
    if route_source and route_source != source:
      raise ValueError('SOURCE_ADDRESS_MISMATCH')
    if not route_source:
      addresses = json.loads(subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', iface],
                                           capture_output=True, text=True, timeout=2, check=True).stdout)
      if not isinstance(addresses, list) or len(addresses) != 1 or not isinstance(addresses[0], dict):
        raise ValueError('SOURCE_ADDRESS_UNVERIFIED')
      assigned = addresses[0].get('addr_info') or []
      if not any(isinstance(entry, dict) and entry.get('family') == 'inet'
                 and entry.get('local') == source for entry in assigned):
        raise ValueError('SOURCE_ADDRESS_UNVERIFIED')
    route_observed['source_verification'] = 'route' if route_source else 'interface_address'
    return {'ok': True, 'interface': iface, 'source': source, 'destination': destination,
            'source_verification': route_observed['source_verification']}
  except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
    # Only enumerated diagnostic reasons; never expose raw command stderr.
    reasons = {'ROUTE_NOT_DIRECT', 'INTERFACE_INVALID', 'CARRIER_DOWN', 'NOT_CDC_NCM',
               'SOURCE_ADDRESS_MISMATCH', 'SOURCE_ADDRESS_UNVERIFIED'}
    reason = str(error) if type(error) is ValueError and str(error) in reasons else (
      'ROUTE_LOOKUP_FAILED' if isinstance(error, subprocess.CalledProcessError) else
      'NCM_DEVICE_UNAVAILABLE' if isinstance(error, OSError) else 'ROUTE_RESULT_INVALID')
    diagnosis = {'ok': False, 'code': 'USB_NCM_UNAVAILABLE',
                 'ncm_reason': reason, 'checked_direction': 'jetson_to_comma' if destination == COMMA else 'comma_to_jetson',
                 'expected_source': source, 'expected_peer': destination,
                 'recommendation': GUIDANCE['USB_NCM_UNAVAILABLE'],
                 'settings_changed': False}
    if route_observed:
      diagnosis['route_observed'] = route_observed
    raise SSHFailure(diagnosis) from error



def comma_host_key_preview(target):
  """Observe the USB peer public host key. The candidate is UNTRUSTED until
  independently compared with the Comma4's own SSH server public key."""
  if not isinstance(target, str) or not TARGET.fullmatch(target) or not target.endswith('@' + COMMA):
    raise ValueError('Only the fixed Comma4 USB SSH peer is supported')
  ncm = ncm_check(target)
  try:
    scan = subprocess.run(['ssh-keyscan', '-T', '3', '-t', 'ed25519', COMMA],
                          capture_output=True, text=True, timeout=5, check=False)
  except (OSError, subprocess.TimeoutExpired):
    return {'ok': False, 'code': 'SSH_HOST_KEY_SCAN_FAILED', 'settings_changed': False,
            'recommendation': 'USB SSH 서버 호스트 공개키를 읽지 못했습니다. 장치 전원과 SSH 서비스를 확인하세요.'}
  keys = set()
  if scan.returncode == 0 and len(scan.stdout) <= 8192:
    for line in scan.stdout.splitlines():
      parts = line.split()
      if len(parts) == 3 and parts[0] == COMMA and parts[1] == 'ssh-ed25519':
        try:
          blob = base64.b64decode(parts[2], validate=True)
          if len(blob) >= 32 and len(blob) <= 1024:
            keys.add(' '.join(parts))
        except ValueError:
          pass
  if len(keys) != 1:
    return {'ok': False, 'code': 'SSH_HOST_KEY_SCAN_FAILED', 'settings_changed': False,
            'recommendation': '콤마4에서 단일 ED25519 호스트 공개키를 확인할 수 없습니다. 자동 신뢰 등록은 하지 않습니다.'}
  key = keys.pop()
  fp = fingerprints(key)
  if len(fp) != 1:
    return {'ok': False, 'code': 'SSH_HOST_KEY_SCAN_FAILED', 'settings_changed': False,
            'recommendation': '호스트 공개키 지문을 계산할 수 없습니다.'}
  return {'ok': True, 'code': 'SSH_HOST_KEY_UNTRUSTED', 'candidate_fingerprint': fp[0],
          'key_type': 'ssh-ed25519', 'source': 'USB ssh-keyscan (not authenticated)',
          'usb_ncm': ncm, 'settings_changed': False, 'trusted': False,
          'recommendation': '후보 지문을 콤마4 장치에서 직접 확인한 SSH 서버 호스트 공개키 지문과 대조해야 합니다.'}


def enroll_comma_host_key(target, expected_fingerprint):
  """Explicit operator-approved ED25519 trust; no identity-key creation or
  StrictHostKeyChecking bypass. Existing host records are never replaced."""
  if not isinstance(expected_fingerprint, str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', expected_fingerprint):
    return {'ok': False, 'code': 'SSH_FINGERPRINT_INVALID', 'settings_changed': False,
            'recommendation': '콤마4 본체에서 확인한 SSH 서버 공개키 SHA256 지문을 정확하게 입력하세요.'}
  preview = comma_host_key_preview(target)
  if preview.get('ok') is not True:
    return preview
  if not hashlib.sha256(expected_fingerprint.encode()).digest() == hashlib.sha256(preview['candidate_fingerprint'].encode()).digest():
    return {'ok': False, 'code': 'SSH_HOST_KEY_MISMATCH', 'settings_changed': False,
            'recommendation': '콤마4 본체 지문과 USB에서 관측된 지문이 다릅니다. 등록하지 않았습니다.'}
  try:
    config = inventory(target)
    home = Path.home()
    directory = home / '.ssh'
    known = directory / 'known_hosts'
    if not any(Path(item['path']) == known for item in config['known_hosts']):
      raise ValueError('SSH 설정의 known_hosts 경로가 기본 경로와 다릅니다.')
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
      raise ValueError('SSH 디렉터리를 신뢰할 수 없습니다.')
    if directory.exists() and stat.S_IMODE(directory.stat().st_mode) & 0o077:
      raise ValueError('SSH 디렉터리의 접근 권한을 먼저 확인하세요.')
    if known.is_symlink() or (known.exists() and not known.is_file()):
      raise ValueError('known_hosts가 일반 파일이 아닙니다.')
    if known.exists() and (stat.S_IMODE(known.stat().st_mode) & 0o022 or known.stat().st_size > 131072):
      raise ValueError('known_hosts 권한 또는 크기가 안전하지 않습니다.')
    if known.exists():
      match = subprocess.run(['ssh-keygen', '-F', COMMA, '-f', str(known)],
                             capture_output=True, text=True, timeout=3, check=False)
      if match.returncode not in (0, 1):
        raise ValueError('기존 호스트키 기록을 확인할 수 없습니다.')
      if match.returncode == 0:
        if expected_fingerprint in fingerprints(match.stdout):
          return {'ok': True, 'code': 'SSH_HOST_ALREADY_TRUSTED', 'settings_changed': False,
                  'trusted': True, 'recommendation': '기존 known_hosts의 호스트키 지문이 일치합니다.'}
        raise ValueError('기존 호스트키 기록이 있습니다. 자동 덮어쓰기는 금지됩니다.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Re-check after creating the directory and never follow a target symlink.
    if directory.is_symlink() or known.is_symlink():
      raise ValueError('SSH 저장 경로가 안전하지 않습니다.')
    original = known.read_text() if known.exists() else ''
    if len(original) > 131072:
      raise ValueError('known_hosts 크기가 너무 큽니다.')
    # We only append the exact key scanned from the fixed USB peer AFTER a
    # human confirms its fingerprint out of band. Other records are preserved.
    fresh = comma_host_key_preview(target)
    if fresh.get('ok') is not True or fresh.get('candidate_fingerprint') != expected_fingerprint:
      raise ValueError('등록 직전 호스트키가 달라졌습니다.')
    scan = subprocess.run(['ssh-keyscan', '-T', '3', '-t', 'ed25519', COMMA],
                          capture_output=True, text=True, timeout=5, check=False)
    matching = [line for line in scan.stdout.splitlines()
                if line.startswith(COMMA + ' ssh-ed25519 ') and fingerprints(line) == [expected_fingerprint]]
    if scan.returncode != 0 or len(set(matching)) != 1:
      raise ValueError('최종 호스트키 검증에 실패했습니다.')
    value = original + ('' if not original or original.endswith('\n') else '\n') + matching[0] + '\n'
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.known-hosts-', delete=False) as stream:
      temp = Path(stream.name)
      os.fchmod(stream.fileno(), 0o600)
      stream.write(value)
    try:
      os.replace(temp, known)
    finally:
      temp.unlink(missing_ok=True)
    return {'ok': True, 'code': 'SSH_HOST_KEY_TRUSTED', 'settings_changed': True,
            'trusted': True, 'fingerprint': expected_fingerprint,
            'recommendation': '호스트 공개키 신뢰 등록 완료. 별도로 젯슨 SSH 로그인 개인키와 콤마4 공개키 승인이 필요합니다.'}
  except (OSError, ValueError, subprocess.TimeoutExpired) as error:
    return {'ok': False, 'code': 'SSH_HOST_KEY_ENROLL_FAILED', 'settings_changed': False,
            'recommendation': '등록 거부: ' + str(error)[:180]}


OFFROAD_SCRIPT = """from pathlib import Path
import json, hashlib, base64
v = {k: Path('/data/params/d', k).read_text().strip() for k in ('IsOffroad', 'IsOnroad')}
try:
  Path('/data/nexo_jetlink_enabled').lstat()
  v['JetlinkEnabled'] = True
except FileNotFoundError:
  v['JetlinkEnabled'] = False
keys = []
for p in (Path.home()/'.ssh/authorized_keys', Path('/data/params/d/GithubSshKeys')):
  try:
    with p.open() as f:
      for line in f.read(65536).splitlines()[:128]:
        parts = line.split()
        for i, item in enumerate(parts[:-1]):
          if item.startswith(('ssh-', 'ecdsa-', 'sk-')):
            try:
              keys.append('SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(parts[i+1], validate=True)).digest()).decode().rstrip('='))
            except ValueError: pass
            break
  except OSError: pass
v['authorized_public_fingerprints'] = sorted(set(keys))[:16]
print(json.dumps(v))
"""


def probe(target, identity_file=None):
  if not isinstance(target, str) or not TARGET.fullmatch(target) or not target.endswith('@' + COMMA):
    raise ValueError('Reverse preflight requires the existing Comma USB user@192.168.100.127')
  direction = 'jetson_to_comma' if target.endswith('@' + COMMA) else 'comma_to_jetson'
  evidence = {'direction': direction, 'checked_monotonic': time.monotonic(), 'settings_changed': False}
  try:
    evidence['client'] = inventory(target, identity_file)
    client = evidence['client']
    evidence['credential_preflight'] = {
      'identity_candidate_present': any(k.get('exists') and k.get('readable')
                                        for k in client.get('identity_files', [])),
      'ssh_agent_present': client.get('agent_available') is True,
      'known_hosts_present': any(k.get('exists') for k in client.get('known_hosts', [])),
      'settings_changed': False,
    }
    evidence['usb_ncm'] = ncm_check(target)
    result = subprocess.run([*ssh_args(target, identity_file), 'python3', '-'], input=OFFROAD_SCRIPT,
                            capture_output=True, text=True, timeout=7, check=False)
    if result.returncode:
      failure = classify_failure(result.stderr, result.returncode, direction=direction)
      client = evidence['client']
      if (failure['code'] == 'SSH_AUTH_DENIED' and not client.get('agent_available')
          and not any(k.get('exists') and k.get('readable') for k in client.get('identity_files', []))):
        failure.update(code='SSH_NO_IDENTITY', recommendation=GUIDANCE['SSH_NO_IDENTITY'])
      return {**evidence, **failure}
    value = json.loads(result.stdout) if len(result.stdout) <= 65536 else None
    if (not isinstance(value, dict) or value.get('IsOffroad') not in ('0', '1')
        or value.get('IsOnroad') not in ('0', '1') or type(value.get('JetlinkEnabled')) is not bool):
      raise ValueError('Invalid state contract')
    allowed = value['IsOffroad'] == '1' and value['IsOnroad'] == '0' and value['JetlinkEnabled'] is False
    return {**evidence, 'ok': True, 'code': 'SSH_VERIFIED', 'authentication_verified': True,
            'public_key_registration_verified': True, 'offroad_allowed': allowed, 'comma_state': value,
            'recommendation': 'SSH 확인 완료' if allowed else 'SSH는 확인됐으나 완전 Offroad 및 외부 AI 비활성이 필요합니다.'}
  except SSHFailure as error:
    return {**evidence, **error.diagnosis, 'direction': direction}
  except subprocess.TimeoutExpired:
    return {**evidence, **classify_failure(direction=direction, timed_out=True)}
  except OSError:
    return {**evidence, 'ok': False, 'code': 'SSH_UNAVAILABLE', 'recommendation': GUIDANCE['SSH_UNAVAILABLE']}
  except (ValueError, TypeError):
    return {**evidence, 'ok': False, 'code': 'SSH_REMOTE_RESPONSE_INVALID',
            'recommendation': GUIDANCE['SSH_REMOTE_RESPONSE_INVALID']}


def save_result(root, result):
  directory = Path(root) / 'cache'
  directory.mkdir(parents=True, exist_ok=True)
  value = {**result, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
  path = directory / 'nexo-comma-ssh-preflight.json'
  with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.ssh-preflight-', delete=False) as stream:
    temporary = Path(stream.name)
    json.dump(value, stream, ensure_ascii=False)
  try:
    temporary.replace(path)
  finally:
    temporary.unlink(missing_ok=True)


class CommaGuard:
  def __init__(self, target, identity_file=None, root=ROOT):
    if not isinstance(target, str) or not TARGET.fullmatch(target) or not target.endswith('@' + COMMA):
      raise ValueError('Engine guard requires the existing Comma USB user@192.168.100.127')
    self.target, self.identity_file, self.root, self.last = target, identity_file, root, {}
  def __call__(self):
    value = probe(self.target, self.identity_file)
    if not self.last or value.get('ok') is not True or value.get('offroad_allowed') is not True:
      save_result(self.root, value)
    self.last = value
    if value.get('ok') is not True:
      raise SSHFailure(value)
    return value.get('offroad_allowed') is True


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--target', required=True)
  parser.add_argument('--identity-file')
  args = parser.parse_args()
  result = probe(args.target, args.identity_file)
  save_result(ROOT, result)
  print(json.dumps(result, ensure_ascii=False))
