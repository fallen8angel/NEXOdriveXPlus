"""Fixed, bounded Jetson management commands; also sent through keyed SSH stdin.

No install, update application, disk erase, shutdown or arbitrary shell command.
"""
import base64
import hashlib
import io
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
            'nexo-jetlink-server.service', 'nexo-yolo.service', 'jetlink-server.service')
ACTIONS = ('status', 'logs', 'settings', 'model', 'check_update', 'restart', 'reconnect', 'video_test', 'hud_trace', 'performance')


def hud_receipt(now=None):
  """Read only receipt/size metadata; never expose JPEG or snapshot contents."""
  clock_now = now is None
  result = {}
  for path in (Path('/dev/shm/nexo-jetson/hud.json'), Path('/dev/shm/carrot-jetlink-hud.packet')):
    try:
      with path.open('rb') as source:
        raw = source.read(512 * 1024 + 1033)
      if len(raw) >= 512 * 1024 + 1033:
        continue
      if path.suffix == '.packet':
        received, = struct.unpack_from('<d', raw)
        value = json.loads(raw[8:])
      else:
        value = json.loads(raw)
        received = value['updated']
      checked = time.monotonic() if clock_now else now
      if value.get('version') != 1 or not 0 <= checked - received < .5:
        continue
      result['hud_rx_age_s'] = checked - received
      for name in ('road', 'wide', 'driver'):
        frame = value.get('cameras', {}).get(name) or {}
        if not frame or not 0 <= value['sent'] - frame['time'] < .3:
          continue
        frame_age = checked - received + value['sent'] - frame['time']
        if frame_age >= .5 or frame.get('width') != 384 or not 2 <= frame['height'] <= 384 or len(frame['jpeg']) > 44 * 1024:
          continue
        try:
          from PIL import Image
        except ImportError:
          return dict(result, decode_error='Jetson 검사 환경에 Pillow가 없습니다.')
        try:
          with Image.open(io.BytesIO(base64.b64decode(frame['jpeg'], validate=True))) as image:
            if image.format != 'JPEG' or image.size != (384, frame['height']):
              continue
            image.load()
        except (ValueError, OSError):
          result['decode_error'] = '수신 JPEG 디코딩 실패'
          continue
        return dict(result, camera_rx_age_s=frame_age, camera_rx_stream=name, decoded=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, struct.error):
      continue
  return result


def installed_model(root):
  current = (root / 'current').resolve()
  manifest = record(current / 'NEXO_RUNTIME.json')
  model = current / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
  sha = None
  try:
    digest = hashlib.sha256()
    with model.open('rb') as source:
      while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    sha = digest.hexdigest()
  except OSError:
    pass
  spec = manifest.get('model') or {}
  engines = []
  if sha:
    for meta in (root / 'cache/engines').glob(sha[:16] + '.*.json'):
      if (record(meta).get('spec') or {}).get('sha256') == sha and meta.with_suffix('.plan').is_file():
        engines.append(meta.stem)
  versions = run(['dpkg-query', '-W', '-f=${Package} ${Version}\n', 'nvidia-l4t-core', 'libnvinfer10'])
  return {'onnx_sha256': sha, 'package_contract': spec, 'matching_engine_files': engines,
          'installed_packages': versions['output'], 'package_runtime': manifest.get('runtime')}


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



def read_hud_trace(path=Path('/dev/shm/nexo-jetson/hud-pipeline-trace.json')):
  """Read bounded HUD pipeline metadata, never raw camera or JPEG images."""
  path = Path(path)
  try:
    if path.is_symlink() or path.stat().st_size > 65536:
      return {'ok': False, 'error': 'HUD 추적 파일 형식 또는 크기를 확인할 수 없습니다.'}
    with path.open('rb') as source:
      raw = source.read(65537)
    if len(raw) > 65536:
      return {'ok': False, 'error': 'HUD 추적 파일 크기 초과'}
    value = json.loads(raw)
    if not isinstance(value, dict):
      return {'ok': False, 'error': 'HUD 추적 데이터가 JSON 객체가 아닙니다.'}
    updated = value.get('updated')
    now = time.monotonic()
    age = round(now - updated, 3) if type(updated) in (float, int) and 0 <= now - updated < 86400 else None
    return {'ok': True, 'hud_pipeline_trace': value, 'trace_age_s': age,
            'note': '읽기 전용 젯슨 HUD 파이프라인 관측입니다. 화면의 물리적 점등은 검증하지 않습니다.'}
  except (OSError, ValueError, UnicodeError) as error:
    return {'ok': False, 'error': f'HUD 추적 파일 확인 불가: {type(error).__name__}'}


# Bounded Jetson AI/HUD performance investigation. This helper is streamed to
# Jetson via SSH stdin, so it works even when the protected HUD image is old.
# No process signals, nvpmodel writes, CUDA calls, GC forcing, or network changes.
def _safe_text(path, limit=8192):
  try:
    with Path(path).open('rb') as stream:
      return stream.read(limit + 1).decode('utf-8', errors='replace')[:limit]
  except (OSError, ValueError):
    return ''


def _safe_number(value):
  try:
    number = float(value)
    return number if 0 <= number < 1e15 else None
  except (TypeError, ValueError, OverflowError):
    return None


def _cpu_counters():
  counters = {}
  for line in _safe_text('/proc/stat', 12288).splitlines():
    items = line.split()
    if not items or not re.fullmatch(r'cpu(?:\d+)?', items[0]):
      continue
    try:
      ticks = [int(v) for v in items[1:9]]
      counters[items[0]] = (sum(ticks), ticks[3] + ticks[4])
    except (ValueError, IndexError):
      continue
  return counters


def _cpu_usage(before, after):
  result = {}
  for name, (total, idle) in after.items():
    old = before.get(name)
    if old and total > old[0] and idle >= old[1]:
      result[name] = round(max(0., min(100., 100 * (1 - (idle - old[1]) / (total - old[0])))), 1)
  return result


def _main_pids(units):
  result = {}
  for name in ('carrot-jetlink.service', 'carrot-jetlink-hud.service', 'nexo-jetlink-server.service',
               'nexo-jetson-hud.service', 'nexo-jetson-host.service'):
    if name not in units or units[name].get('ActiveState') != 'active':
      continue
    try:
      raw = run(['systemctl', 'show', name, '--value', '-p', 'MainPID'], timeout=1.5)
      number = raw['output'].strip()
      if raw['code'] == 0 and number.isdecimal() and 1 < int(number) <= 4194304:
        result[name] = int(number)
    except (OSError, subprocess.SubprocessError):
      continue  # An unresponsive unit must not discard all other diagnostics.
  return result


def _proc_memory(pid):
  fields = {'VmRSS': 'rss_kb', 'VmHWM': 'peak_rss_kb', 'VmSwap': 'swap_kb',
            'RssAnon': 'anonymous_kb', 'RssFile': 'file_kb', 'RssShmem': 'shared_kb',
            'Threads': 'threads', 'voluntary_ctxt_switches': 'voluntary_context_switches',
            'nonvoluntary_ctxt_switches': 'involuntary_context_switches'}
  result = {}
  for line in _safe_text(f'/proc/{pid}/status', 16384).splitlines():
    key, _, value = line.partition(':')
    if key in fields:
      digits = value.strip().split()
      if digits and digits[0].isdecimal():
        result[fields[key]] = int(digits[0])
  for line in _safe_text(f'/proc/{pid}/smaps_rollup', 8192).splitlines():
    key, _, value = line.partition(':')
    if key in ('Pss', 'Private_Dirty', 'SwapPss'):
      parts = value.strip().split()
      if parts and parts[0].isdecimal():
        result[{'Pss': 'pss_kb', 'Private_Dirty': 'private_dirty_kb',
                'SwapPss': 'swap_pss_kb'}[key]] = int(parts[0])
  return result


def _thread_counters(pids):
  rows = {}
  for role, pid in list(pids.items())[:5]:
    for path in sorted(Path(f'/proc/{pid}/task').glob('[0-9]*'))[:48]:
      tid = int(path.name)
      parts = _safe_text(path / 'schedstat', 128).split()
      if len(parts) >= 2 and all(value.isdecimal() for value in parts[:2]):
        rows[(role, tid)] = (int(parts[0]), int(parts[1]))
  return rows


def _thread_usage(before, after, elapsed):
  rows = []
  if not 0.05 <= elapsed <= 3:
    return rows
  for (role, tid), (cpu, wait) in after.items():
    old = before.get((role, tid))
    if old is None or cpu < old[0] or wait < old[1]:
      continue
    rows.append({'service': role, 'tid': tid, 'cpu_pct_one_core': round((cpu-old[0]) / 1e7 / elapsed, 2),
                 'runqueue_wait_ms': round((wait-old[1])/1e6, 3),
                 'cpu_time_ms': round((cpu-old[0])/1e6, 3), 'interval_s': round(elapsed, 3)})
  rows.sort(key=lambda x: (x['runqueue_wait_ms'], x['cpu_pct_one_core']), reverse=True)
  return rows[:12]


def _gpu_evidence():
  clocks = []
  for path in sorted(Path('/sys/class/devfreq').glob('*'))[:80]:
    name = path.name.lower()
    if not any(word in name for word in ('gpu', 'ga10b', 'gv11b', '17000000', '57000000')):
      continue
    metrics = {}
    for key, filename in (('clock_mhz','cur_freq'), ('min_clock_mhz','min_freq'), ('max_clock_mhz','max_freq')):
      value = _safe_number(_safe_text(path / filename, 128).strip())
      metrics[key] = round(value / 1e6, 2) if value is not None else None
    value = _safe_number(_safe_text(path / 'device/load', 128).strip())
    metrics['load_pct'] = round(value / 10, 1) if value is not None and value <= 1000 else None
    clocks.append({'device': name[:90], **metrics})
    if len(clocks) >= 4:
      break
  base = Path('/sys/kernel/debug/bpmp/debug/clk/emc')
  emc = _safe_number(_safe_text(base / 'rate', 128).strip())
  return {'gpu': clocks, 'emc_clock_mhz': round(emc / 1e6, 2) if emc is not None else None,
          'gpu_available': bool(clocks), 'note': 'GPU 부하·EMC는 드라이버/권한이 허용한 경우만 기록합니다.'}


def _power_mode():
  try:
    value = run(['/usr/sbin/nvpmodel', '-q'], timeout=1)
    match = re.search(r'NV Power Mode:\s*([^\r\n]+)\s*\n\s*(\d+)', value['output'])
    return {'mode': match[1].strip()[:72], 'id': int(match[2])} if match else {'available': False}
  except (OSError, subprocess.SubprocessError):
    return {'available': False}


def _meminfo():
  allowed = ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree', 'Cached', 'SReclaimable')
  result = {}
  for line in _safe_text('/proc/meminfo', 8192).splitlines():
    key, _, value = line.partition(':')
    bits = value.strip().split()
    if key in allowed and bits and bits[0].isdecimal():
      result[key.lower() + '_kb'] = int(bits[0])
  return result


def _python_gc():
  # Produced in a separate observer thread in the Jetson HUD renderer only.
  path = Path('/dev/shm/nexo-jetson/hud-python-gc.json')
  if path.is_symlink():
    return {'available': False, 'reason': 'untrusted_gc_path'}
  value = record(path)
  stamp, pid = value.get('updated'), value.get('pid')
  now = time.monotonic()
  if (type(pid) is not int or not 1 < pid <= 4194304 or
      type(stamp) not in (int, float) or not 0 <= now-stamp < 15 or
      not Path(f'/proc/{pid}').is_dir()):
    return {'available': False, 'reason': 'hud_gc_observer_unavailable_or_stale'}
  return {'available': True, 'age_s': round(now-stamp, 2), 'pid': pid,
          'generations': (value.get('generations') or [])[:3],
          'recent_max_pause_ms': value.get('recent_max_pause_ms'),
          'gc_pause_total_ms': value.get('gc_pause_total_ms'),
          'gc_enabled': value.get('gc_enabled') if type(value.get('gc_enabled')) is bool else None,
          'sample_interval_s': value.get('sample_interval_s'),
          'monitoring_only': True}


def performance_sample(units, root, interval=.35):
  """Read-only on-demand CPU/GPU/memory/scheduler/GC + HUD evidence.

  Process/thread deltas use the SAME Jetson monotonic clock. No Linux
  device is modified. Missing samples are UNKNOWN, never fake zero.
  """
  interval = max(.2, min(.8, float(interval)))
  pids = _main_pids(units)
  gc = _python_gc()
  if gc.get('available') is True and gc['pid'] not in pids.values():
    pids['nexo-hud-renderer'] = gc['pid']
  cpu_before, thread_before, started = _cpu_counters(), _thread_counters(pids), time.monotonic()
  time.sleep(interval)
  elapsed = time.monotonic()-started
  cpu_after, thread_after = _cpu_counters(), _thread_counters(pids)
  processes = [{'service': service, 'pid': pid, **_proc_memory(pid)}
               for service, pid in pids.items()]
  hud_trace = read_hud_trace()
  trace = hud_trace.get('hud_pipeline_trace') or {} if hud_trace.get('ok') else {}
  hud = {key: trace.get(key) for key in (
      'input_mode', 'input_reason', 'input_age_s', 'encoded_age_s',
      'panel_write_age_s', 'panel_gap_count', 'panel_gap_max_s',
      'encoder_sent_count', 'encoder_expired_count', 'waiting_episodes',
      'waiting_max_s', 'panel_gap_threshold_s') if key in trace}
  control = control_state(root)
  engine = control.get('engine', {})
  return {'ok': True, 'performance': {
    'schema_version': 1, 'read_only': True,
    'sample_monotonic': round(time.monotonic(), 3), 'interval_s': round(elapsed, 3),
    'cpu_busy_pct': _cpu_usage(cpu_before, cpu_after),
    'clocks': _gpu_evidence(), 'power_mode': _power_mode(),
    'memory': _meminfo(), 'processes': processes,
    'server_scheduler_threads': _thread_usage(thread_before, thread_after, elapsed),
    'scheduler_observation_available': bool(thread_after),
    'hud_python_gc': gc, 'hud_pipeline': hud, 'hud_trace_age_s': hud_trace.get('trace_age_s'),
    'engine': {key: engine[key] for key in ('state','detail') if key in engine},
    'limitations': [
      'A short CPU/scheduler sample cannot establish the cause of a specific historical frame stall.',
      'GPU load and clocks require readable Jetson driver interfaces.',
      'GC observations cover only the Jetson HUD Python renderer, not native TensorRT/CUDA pauses.',
      'Reported panel writes do not verify physical screen illumination.',
      'Jetson model readiness does not prove external driving inference without fresh comma modeld evidence.',
    ],
  }}


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
  native = units.get('nexo-jetlink-server.service', {})
  if native.get('ActiveState') == 'active' and native.get('WorkingDirectory'):
    return Path(native['WorkingDirectory']).resolve()
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


def installed_source_commit(root):
  """Normalize the protected nexo-<SHA> release and its SOURCE_COMMIT marker."""
  try:
    value = (root / 'current/SOURCE_COMMIT').read_text(encoding='utf-8').strip()
    if re.fullmatch('[0-9a-f]{40}', value):
      return value
  except OSError:
    pass
  name = (root / 'current').resolve().name
  if name.startswith('nexo-'):
    name = name[5:]
  return name if re.fullmatch('[0-9a-f]{40}', name) else ''


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
             'service_active': (units['carrot-jetlink.service'].get('ActiveState') == 'active'
                                if 'carrot-jetlink.service' in units else None),
             'comma_connected': link.get('state') == 'connected' if link else None,
             'camera_frame_seen': frame_recent, 'model_active': yolo_recent,
             'model_ready': engine.get('state') == 'ready' if engine else None,
             'last_error': engine.get('detail', '') if engine.get('state') == 'failed' else '',
             'pipeline': 'SSH 관리 · ' + ', '.join(units), **usb_devices(), **hud_receipt()}
  version = installed_source_commit(root)
  if version:
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
  if not units and action not in ('model', 'video_test', 'hud_trace', 'performance'):
    raise ValueError('supported Jetson service not installed')
  root = root_for(units)
  if action == 'hud_trace':
    return read_hud_trace()
  if action == 'performance':
    return performance_sample(units, root)
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
  if action == 'video_test':
    first = hud_receipt()
    latest = first
    deadline = time.monotonic() + 2
    while not latest.get('decoded') and time.monotonic() < deadline:
      time.sleep(.12)
      latest = hud_receipt()
    seen = latest.get('decoded') is True
    return {'ok': True, 'video_received': seen, 'before': first, 'after': latest, 'decode_error': latest.get('decode_error'),
            'note': '최근 JPEG 수신 및 디코딩 확인' if seen else '2초 관찰에서 최근 수신 영상을 확인하지 못했습니다.'}
  first = collect(units, root)
  if action == 'model':
    return {'ok': True, 'installed_model': installed_model(root), 'services': first['services'],
            'engine': first['control'].get('engine'), 'link': first['control'].get('link')}
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
