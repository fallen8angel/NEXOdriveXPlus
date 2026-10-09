"""Optional companion telemetry, independent of native cereal/Params/model imports."""
import ipaddress
import math
from pathlib import Path
import time

from openpilot.tools.jetson.state import decode_json

DISPLAY_STATUS = Path('/dev/shm/nexo-jetson/status.json')
LINK_STATUS = (Path('/dev/shm/nexo-jetlink.json'), Path('/dev/shm/carrot-jetlink.json'))
MODEL_STATUS = (Path('/dev/shm/nexo-jetlink-model.json'), Path('/dev/shm/carrot-jetlink-model.json'))
UDC_ROOT = Path('/sys/class/udc')


def read_record(path):
  try:
    with Path(path).open('rb') as source:
      return decode_json(source.read(65537), 65536)
  except (OSError, ValueError, TypeError, RecursionError):
    return {}


def age(value, now):
  if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= now - value:
    return now - value
  return None


def fresh(value, now, seconds=3):
  delta = age(value.get('updated'), now)
  return delta is not None and delta < seconds


def address(value):
  if not isinstance(value, str):
    return ''
  try:
    parsed = ipaddress.ip_address(value)
    return str(parsed) if not (parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast or parsed.is_link_local) else ''
  except (ValueError, TypeError):
    return ''


def usb_status(root=None):
  result = {'connected': None, 'usb3': None, 'speed': '확인 불가'}
  for controller in (root or UDC_ROOT).glob('*'):
    try:
      configured = (controller / 'state').read_text().strip() == 'configured'
      if not configured:
        if result['connected'] is None:
          result['connected'] = False
        continue
      speed = (controller / 'current_speed').read_text().strip()
      result.update(connected=True, speed=speed, usb3=speed in ('super-speed', 'super-speed-plus', 'super speed', 'super speed plus'))
      return result
    except OSError:
      pass
  return result


def reported(values):
  if any(v is True for v in values):
    return True
  return False if any(v is False for v in values) else None


def snapshot(state, now=None):
  now = time.monotonic() if now is None else now
  candidates = []
  last_receive = []
  for channel, entry in state.get('channels', {}).items():
    delta = age(entry.get('received_mono'), now)
    if delta is not None:
      last_receive.append(delta)
    if delta is not None and delta < 5:
      packet = entry['packet']
      alive = packet.get('usb_connected') is True if channel == 'usb' else True
      if alive:
        candidates.append((channel, packet, entry.get('source_ip', ''), delta))
  display = read_record(DISPLAY_STATUS)
  if fresh(display, now) and display.get('usb_connected') is True:
    candidates.append(('usb', display, '', age(display['updated'], now)))
  link = next((v for p in LINK_STATUS if fresh(v := read_record(p), now)), {})
  model = next((v for p in MODEL_STATUS if fresh(v := read_record(p), now)), {})
  evidence = [age(link.get(key), now) for key in ('telemetry_updated', 'last_infer_monotonic')]
  link_live = link.get('state') == 'ready' and any(delta is not None and delta < 3 for delta in evidence)
  if link_live:
    telemetry = link.get('telemetry') or link.get('peer') or {}
    candidates.append(('usb', {'usb_connected': True, 'comma_connected': True, 'service_active': True,
                               'protocol': 'carrot-v2', 'host_telemetry': telemetry,
                               'model_ready': model.get('ready'), 'model_active': model.get('active'),
                               'inference_age_s': age(link.get('last_infer_monotonic'), now)}, '', age(link['updated'], now)))
  connected = bool(candidates)
  combined = {}
  ip = ''
  for _, packet, source, _ in candidates:
    combined.update(packet)
    ip = address(source) or address(packet.get('ip')) or ip
  telemetry = combined.get('host_telemetry')
  if not isinstance(telemetry, dict):
    telemetry = {}
  # Carrot HELLO wraps telemetry; STATE carries it at the top level.
  telemetry = telemetry.get('telemetry', telemetry)
  if not isinstance(telemetry, dict):
    telemetry = {}
  health = telemetry.get('carrot_health')
  if not isinstance(health, dict):
    health = {}
  health_age = age(link.get('telemetry_updated'), now) if link_live else (candidates[-1][3] if candidates else None)
  remote_age = health.get('age_s')
  health_fresh = (isinstance(remote_age, (int, float)) and not isinstance(remote_age, bool) and math.isfinite(remote_age)
                  and health_age is not None and 0 <= remote_age + health_age < 3)
  if health_fresh:
    addresses = health.get('addresses')
    for entry in (addresses[:8] if isinstance(addresses, list) else []):
      if isinstance(entry, dict):
        ip = address(entry.get('address')) or ip
  error = str(link.get('error') or model.get('error') or combined.get('last_error') or '')[:300]
  if health_fresh and health.get('severity') == 'error':
    error = str(health.get('reason') or 'Jetson host health error')[:300]
  state_name = '오류' if error and (connected or link) else '정상' if connected else '연결 중' if link.get('state') in (
    'connecting', 'loading', 'retrying', 'waiting_model_contract') else '미연결'
  usb = usb_status()
  remote_usb = next((p for c, p, _, _ in candidates if c == 'ssh'), {})
  if isinstance(remote_usb.get('usb3'), bool) and usb['usb3'] is None:
    usb.update(usb3=remote_usb['usb3'], speed=remote_usb.get('usb_speed', '확인 불가'))
  elif not any(c == 'usb' for c, _, _, _ in candidates):
    usb.update(usb3=None, speed='미연결')
  frames = []
  for _, packet, _, _ in candidates:
    inference_age = packet.get('inference_age_s')
    valid_age = (isinstance(inference_age, (int, float)) and not isinstance(inference_age, bool)
                 and math.isfinite(inference_age) and inference_age >= 0)
    frames.append(inference_age < 3 if valid_age else packet.get('camera_frame_seen', packet.get('frame_seen')))
  frame_recent = reported(frames)
  active = reported([p.get('model_active', p.get('yolo_recent')) for _, p, _, _ in candidates])
  comma_states = [True if c == 'usb' else p.get('comma_connected', p.get('comma_tcp')) for c, p, _, _ in candidates]
  comma_connected = reported(comma_states)
  combined.update(ip=ip, comma_connected=comma_connected,
                  usb_connected=any(c == 'usb' or p.get('usb_connected') is True for c, p, _, _ in candidates),
                  usb3=usb['usb3'], usb_speed=usb['speed'],
                  camera_frame_seen=frame_recent, model_active=active,
                  model_ready=reported([p.get('model_ready', p.get('ready')) for _, p, _, _ in candidates]),
                  service_active=reported([p.get('service_active') for _, p, _, _ in candidates]),
                  last_error=error, state=state_name)
  # Never retain old TRUE values/IP when every independent lease expires.
  if not connected:
    combined = {'state': state_name, 'last_error': error, 'usb_speed': usb['speed'], 'usb3': usb['usb3']}
  deltas = [c[3] for c in candidates] or last_receive
  return {'connected': connected, 'state': state_name, 'status': combined,
          'age_ms': int(min(deltas) * 1000) if deltas else None,
          'native_link': {'state': link.get('state', 'unavailable'), 'model': link.get('model', ''),
                          'model_ready': model.get('ready'), 'model_active': model.get('active')},
          'diagnosis': error or combined.get('diagnosis') or ('상태 수신 중' if connected else '최근 상태 신호 없음')}
