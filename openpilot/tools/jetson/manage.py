"""Fixed, bounded Jetson management commands; also sent through keyed SSH stdin.

No install, update application, disk erase, shutdown or arbitrary shell command.
"""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request

MANIFEST_URL = 'https://upload.shind0.synology.me/models/jetlink-host-stable/manifest.json'

SERVICES = ('carrot-jetlink.service', 'carrot-jetlink-hud.service', 'nexo-jetson-host.service', 'nexo-jetson-hud.service',
            'nexo-yolo.service', 'jetlink-server.service')
ACTIONS = ('status', 'logs', 'settings', 'model', 'check_update', 'restart', 'reconnect', 'video_test')


def hud_receipt(now=None):
  """Read only receipt/size metadata; never expose JPEG or snapshot contents."""
  now = time.monotonic() if now is None else now
  path = Path('/dev/shm/carrot-jetlink-hud.packet')
  try:
    with path.open('rb') as source:
      raw = source.read(512 * 1024 + 9)
    if not 8 < len(raw) <= 512 * 1024 + 8:
      return {}
    received, = struct.unpack_from('<d', raw)
    if not 0 <= now - received < .5:
      return {}
    value = json.loads(raw[8:])
    if not isinstance(value, dict) or value.get('version') != 1:
      return {}
    result = {'hud_rx_age_s': now - received}
    cameras = value.get('cameras', {})
    for name in ('road', 'wide'):
      frame = cameras.get(name, {})
      if not isinstance(frame, dict) or not isinstance(value.get('sent'), (int, float)) or not isinstance(frame.get('time'), (int, float)):
        continue
      delta = value['sent'] - frame['time']
      if (0 <= delta < .3 and frame.get('width') == 384 and frame.get('height') == 240
          and isinstance(frame.get('jpeg'), str) and 0 < len(frame['jpeg']) <= 28 * 1024):
        result.update(camera_rx_age_s=now - received + delta, camera_rx_stream=name)
        break
    return result
  except (OSError, ValueError, KeyError, TypeError, AttributeError):
    return {}


def usb_role_policy():
  # The installed FUSB301 driver owns role negotiation; diagnostics never write it.
  base = Path('/sys/bus/i2c/devices/1-0025/fusb301')
  value = {}
  for name in ('fmode', 'fsw_trysnk'):
    try:
      text = (base / name).read_text().strip()
      if re.fullmatch(r'(?:SRC|SRC\+ACC|SNK|SNK\+ACC|DRP|DRP\+ACC)\(\d+\)|[01]', text):
        value[name] = text
    except OSError:
      pass
  return value


def run(args, timeout=3):
  result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
  return {'code': result.returncode, 'output': (result.stdout + result.stderr)[-16000:]}


def record(path):
  try:
    with Path(path).open('rb') as source:
      value = json.loads(source.read(65537))
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


def services():
  found = {}
  for unit in SERVICES:
    result = run(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
                  '-p', 'UnitFileState', '-p', 'WorkingDirectory', '-p', 'ExecStart'])
    values = dict(line.split('=', 1) for line in result['output'].splitlines() if '=' in line)
    if values.get('LoadState') == 'loaded':
      # ExecStart is used internally to find the runtime, not exposed as settings/secrets.
      found[unit] = values
  return found


def root_for(units):
  directory = units.get('carrot-jetlink.service', {}).get('WorkingDirectory', '')
  if directory:
    root = Path(directory).resolve()
    # Runtime layout from Windows image; versioned runtime sits below current.
    for parent in (root, *root.parents):
      if (parent / 'cache').is_dir() and (parent / 'updates').is_dir():
        return parent
  return Path('/opt/carrot-jetlink')


def control_state(root):
  result = {}
  path = root / 'control.sock'
  if not path.exists():
    return result
  try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
      sock.settimeout(.5)
      sock.connect(str(path))
      pending = b''
      deadline = time.monotonic() + 1
      while time.monotonic() < deadline and len(pending) < 65536:
        raw = sock.recv(8192)
        if not raw:
          break
        pending += raw
        while b'\n' in pending:
          line, pending = pending.split(b'\n', 1)
          event = json.loads(line)
          if event.get('event') in ('server', 'link', 'engine'):
            result[event['event']] = {k: v for k, v in event.items() if k not in ('event', 't')}
        if all(key in result for key in ('server', 'link', 'engine')):
          break
  except (OSError, ValueError):
    pass
  return result


def usb_devices():
  for device in Path('/sys/bus/usb/devices').glob('*'):
    try:
      if (device / 'idVendor').read_text().strip() != '1209' or (device / 'idProduct').read_text().strip() != '0001':
        continue
      speed = float((device / 'speed').read_text())
      return {'usb_connected': True, 'usb3': speed >= 5000, 'usb_speed': f'{speed:g} Mbps'}
    except (OSError, ValueError):
      pass
  return {'usb_connected': False, 'usb3': None, 'usb_speed': '미연결'}


def latest_carrot(root, units):
  # Only inspect metadata. Download/stage/activation functions are never called.
  with urllib.request.urlopen(MANIFEST_URL, timeout=3) as response:
    if response.url != MANIFEST_URL:
      raise ValueError('unexpected manifest redirect')
    raw = response.read(256 * 1024 + 1)
  if len(raw) > 256 * 1024:
    raise ValueError('manifest exceeded size limit')
  manifest = json.loads(raw)
  source = manifest.get('source_commit')
  if manifest.get('format') != 1 or not isinstance(source, str) or not re.fullmatch('[0-9a-f]{40}', source):
    raise ValueError('invalid update manifest identity')
  verified = False
  verification_error = ''
  try:
    import base64
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa
    cwd = Path(units.get('carrot-jetlink.service', {}).get('WorkingDirectory', str(root)))
    key_path = next(p for p in (cwd / 'tools/jetlink/release-signing-public.pem',
                                root / 'current/tools/jetlink/release-signing-public.pem') if p.is_file())
    key = ECC.import_key(key_path.read_text())
    payload = json.dumps({k: v for k, v in manifest.items() if k != 'signature'}, sort_keys=True, separators=(',', ':')).encode()
    eddsa.new(key, 'rfc8032').verify(payload, base64.b64decode(manifest['signature'], validate=True))
    verified = True
  except (ImportError, OSError, ValueError, KeyError, StopIteration) as error:
    verification_error = f'{type(error).__name__}: {error}'[:240]
  installed = (root / 'current').resolve().name
  return {'source_commit': source, 'signature_verified': verified, 'verification_error': verification_error,
          'available': source != installed if verified and re.fullmatch('[0-9a-f]{40}', installed) else None,
          'applied': False}


def collect(units, root):
  now = time.monotonic()
  files = {}
  for name, path in (
    ('health', '/dev/shm/carrot-jetlink-health.json'),
    ('network', '/dev/shm/carrot-jetlink-network-status.json'),
    ('display', '/dev/shm/nexo-jetson/status.json'),
    ('video', '/dev/shm/nexo-jetson/video-input.json'),
    ('yolo', '/dev/shm/nexo-jetson/yolo.json'),
    ('direct', '/tmp/nexo-yolo-direct-status.json'),
  ):
    value = record(path)
    stamp = value.get('updated')
    if name == 'direct':
      # Legacy worker stores wall time on Jetson. Compare only with that host's clock.
      stamp = value.get('updated_ts')
      current = time.time()  # noqa: TID251
    else:
      current = now
    if isinstance(stamp, (int, float)) and 0 <= current - stamp < 5:
      files[name] = value
  control = control_state(root)
  frame = files.get('direct', {})
  if frame:
    frame_stamp = frame.get('last_frame_ts')
    yolo_stamp = frame.get('last_yolo_ts')
    frame_recent = isinstance(frame_stamp, (int, float)) and 0 <= time.time() - frame_stamp < 3  # noqa: TID251
    yolo_recent = isinstance(yolo_stamp, (int, float)) and 0 <= time.time() - yolo_stamp < 3  # noqa: TID251
  else:
    frame_recent = None
    yolo_recent = True if 'yolo' in files else None
  engine = control.get('engine', {})
  link = control.get('link', {})
  summary = {'magic': 'NEXO_JETSON_STATUS', 'host': socket.gethostname(),
             'service_active': any(v.get('ActiveState') == 'active' for v in units.values()),
             'comma_connected': link.get('state') == 'connected' if link else None,
             'camera_frame_seen': frame_recent, 'model_active': yolo_recent,
             'model_ready': engine.get('state') == 'ready' if engine else None,
             'last_error': engine.get('detail', '') if engine.get('state') == 'failed' else '',
             'pipeline': 'SSH 관리 · ' + ', '.join(units), **usb_devices(), **hud_receipt(now)}
  version = (root / 'current').resolve().name
  if re.fullmatch('[0-9a-f]{40}', version):
    summary['jetson_version'] = version
  health = files.get('health', {})
  if health:
    summary['host_telemetry'] = {'carrot_health': dict(health, age_s=now - health['updated'])}
  return {'summary': summary, 'services': {unit: {k: v for k, v in info.items() if k in ('ActiveState', 'SubState', 'UnitFileState')}
                       for unit, info in units.items()},
          'runtime': str(root), 'control': control, 'telemetry': files,
          'model_cache': record(root / 'cache/last-loaded.json'), 'usb_role_policy': usb_role_policy(),
          'update': record(root / 'updates/status.json'), 'sleep': '수동 확인 필요 · 전원 정책 변경 없음'}


def handle(action):
  if action not in ACTIONS:
    raise ValueError('unsupported management action')
  if not Path('/etc/nv_tegra_release').is_file():
    raise ValueError('SSH target is not a Jetson; operation refused')
  units = services()
  if not units:
    raise ValueError('supported Jetson service not installed')
  root = root_for(units)
  if action in ('restart', 'reconnect'):
    if '--confirmed-offroad' not in sys.argv:
      raise ValueError('parked confirmation required')
    # Restart the running owners; preserve enablement, cache and host configuration.
    selected = [unit for unit, info in units.items() if info.get('ActiveState') == 'active']
    if not selected:
      selected = [next(iter(units))]
    result = run(['sudo', '-n', 'systemctl', 'restart', *selected], timeout=8)
    if result['code'] != 0:
      raise RuntimeError(result['output'] or 'service restart failed (sudo permission required)')
    return {'ok': True, 'restarted': selected, 'status': collect(services(), root)}
  if action == 'logs':
    result = run(['journalctl', '--no-pager', '-n', '100', '-o', 'short-iso', *[arg for unit in units for arg in ('-u', unit)]])
    lines = result['output'].splitlines()
    sanitized = ['[민감 정보 포함 행 숨김]' if re.search(r'password|passwd|psk|secret|token|authorization', line, re.I) else line
                 for line in lines]
    return {'ok': result['code'] == 0, 'logs': '\n'.join(sanitized)}
  if action == 'check_update':
    # Read-only comparison: stock Carrot's signed updater is never run here.
    value = {'installed': str((root / 'current').resolve()), 'staged': record(root / 'updates/status.json'),
             'pending': bool((root / 'updates/pending.json').exists()), 'applied': False,
             'note': '현재 설치·대기 업데이트 상태입니다. 업데이트 적용은 이 메뉴에서 실행하지 않습니다.'}
    if 'jetlink-server.service' in units:
      value['latest'] = run(['jetlink', 'update', '--check'], timeout=4)
    elif 'carrot-jetlink.service' in units:
      try:
        value['latest'] = latest_carrot(root, units)
      except (OSError, ValueError) as error:
        value['latest'] = {'available': None, 'error': str(error)[:240], 'applied': False}
    return {'ok': True, 'update': value}
  first = collect(units, root)
  if action == 'video_test':
    time.sleep(2)
    second = collect(services(), root)
    # A service or cached model is never proof of recent received video.
    return {'ok': True, 'before': first, 'after': second,
            'note': '2초 관찰 결과입니다. onroad 영상이 없으면 수신 성공을 의미하지 않습니다.'}
  if action == 'settings':
    # Deliberately omit Wi-Fi profiles, environment files, passwords and SSH keys.
    first['settings'] = {'transport': 'USB host', 'runtime': str(root),
                         'protected_storage': Path('/etc/carrot-jetlink-protected.json').exists(),
                         'service_auto_start': {u: v.get('UnitFileState', 'unknown') for u, v in units.items()}}
  return {'ok': True, 'status': first}


if __name__ == '__main__':
  try:
    result = handle(sys.argv[1] if len(sys.argv) > 1 else 'status')
  except Exception as error:
    result = {'ok': False, 'error': f'{type(error).__name__}: {error}'[:400]}
  print(json.dumps(result, ensure_ascii=False, allow_nan=False))
