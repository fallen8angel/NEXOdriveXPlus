"""Optional companion telemetry, independent of native cereal/Params/model imports."""
import ipaddress
import math
from pathlib import Path
import time

from openpilot.tools.jetson.state import decode_json, finite, public_telemetry, public_text

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
  error = public_text(link.get('error') or model.get('error') or combined.get('last_error')
                      or (display.get('last_error') if fresh(display, now) else ''), 300)
  if health_fresh and health.get('severity') == 'error':
    error = public_text(health.get('reason') or 'Jetson host health error', 300)
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
  details = pipeline_details(candidates, combined, link, health if health_fresh else {}, usb, now)
  combined['host_telemetry'] = public_telemetry(combined.get('host_telemetry'))
  # Never retain old TRUE values/IP when every independent lease expires.
  if not connected:
    combined = {'state': state_name, 'last_error': error, 'usb_speed': usb['speed'], 'usb3': usb['usb3']}
  deltas = [c[3] for c in candidates] or last_receive
  return {'connected': connected, 'state': state_name, 'status': combined, **details,
          'age_ms': int(min(deltas) * 1000) if deltas else None,
          'native_link': {'state': link.get('state', 'unavailable'), 'model': link.get('model', ''),
                          'model_ready': model.get('ready'), 'model_active': model.get('active')},
          'diagnosis': error or combined.get('diagnosis') or ('상태 수신 중' if connected else '최근 상태 신호 없음')}


def pipeline_details(candidates, status, link, health, usb, now):
  """Observation only: TX, peer health and inference never authorize controls."""
  live = bool(candidates)
  local = [packet for channel, packet, _, _ in candidates if channel == 'usb']

  def local_age(key):
    values = [delta for packet in local if (delta := age(packet.get(key), now)) is not None]
    return min(values) if values else None

  def remote_age(key):
    values = [packet[key] + delta for _, packet, _, delta in candidates
              if finite(packet.get(key)) and packet[key] >= 0]
    return min(values) if values else None

  heartbeat = local_age('last_receive_monotonic')
  if heartbeat is None and live:
    values = [delta for delta in (age(link.get('telemetry_updated'), now), age(link.get('last_infer_monotonic'), now)) if delta is not None]
    heartbeat = min(values) if values else min(candidate[3] for candidate in candidates)
  recent = {'heartbeat_age_s': heartbeat, 'hud_tx_age_s': local_age('last_hud_tx_mono'),
            'camera_tx_age_s': local_age('last_camera_tx_mono'), 'camera_frame_age_s': local_age('camera_frame_mono'),
            'inference_age_s': remote_age('inference_age_s'), 'camera_rx_age_s': remote_age('camera_rx_age_s')}
  if link.get('state') == 'ready' and local:
    recent['inference_age_s'] = age(link.get('last_infer_monotonic'), now)
  if recent['inference_age_s'] is None:
    recent['inference_age_s'] = remote_age('yolo_age_ms')
    if recent['inference_age_s'] is not None:
      # yolo_age_ms is in milliseconds; each channel's receive delta is seconds.
      recent['inference_age_s'] = min(packet['yolo_age_ms'] / 1000 + delta for _, packet, _, delta in candidates
                                     if finite(packet.get('yolo_age_ms')) and packet['yolo_age_ms'] >= 0)
  hud_tx = recent['hud_tx_age_s'] is not None and recent['hud_tx_age_s'] < .5
  camera_tx = (recent['camera_tx_age_s'] is not None and recent['camera_tx_age_s'] < .5
               and recent['camera_frame_age_s'] is not None and recent['camera_frame_age_s'] < .5)
  camera_rx = recent['camera_rx_age_s'] is not None and recent['camera_rx_age_s'] < .5
  session = any(p.get('session_connected') is True or p.get('usb_connected') is True for p in local)
  negotiation = any(p.get('capability_negotiated') is True for p in local) or link.get('state') == 'ready' and session
  hud_rx = any(p.get('hud_connected') is True for _, p, _, _ in candidates)
  for _, packet, _, delta in candidates:
    if delta < 3:
      telemetry = public_telemetry(packet.get('host_telemetry'))
      telemetry = telemetry.get('telemetry', telemetry)
      hud_rx |= telemetry.get('carrot_hud_connected') is True
  display_error = public_text(status.get('display_error'))

  def stage(key, label, ok, detail='', fault=''):
    state = 'ERROR' if fault else 'OK' if ok else 'WAITING' if live else 'DISCONNECTED'
    return {'id': key, 'label': label, 'state': state, 'detail': fault or detail}

  stages = [stage('jetson', 'Jetson', live),
            stage('usb', 'USB', status.get('usb_connected') is True or live and usb['connected'] is True, usb['speed']),
            stage('jetlink', 'Jetlink', session, 'capability 협상 완료' if negotiation else 'legacy 경로 또는 협상 확인 대기',
                  status.get('last_error') if not session else ''),
            stage('camera', 'Camera', camera_tx or camera_rx or status.get('camera_frame_seen') is True,
                  'TX 완료' if camera_tx else 'RX 관측' if camera_rx else '최근 영상 확인 대기'),
            stage('inference', 'Inference', status.get('model_active') is True,
                  '엔진 준비 · 추론 대기' if status.get('model_ready') is True else '준비 또는 추론 확인 대기'),
            stage('hud', 'HUD', hud_tx or hud_rx, 'TX 완료' if hud_tx else 'HUD 수신/표시 관측' if hud_rx else '표시 데이터 대기', display_error)]
  temperature = health.get('temp_c')
  if temperature is None:
    temperature = next((packet.get('temperature_c') for _, packet, _, delta in sorted(candidates, key=lambda c: c[3])
                        if delta < 3 and finite(packet.get('temperature_c'))), None)
  temperature = temperature if finite(temperature) and -40 <= temperature <= 150 else None
  info = {'temperature_c': temperature, 'thermal': public_text(health.get('severity')) or 'unknown',
          'storage': public_text(health.get('storage_mode')) or 'unknown',
          'version': public_text(status.get('jetson_version'), 128) or 'unknown',
          'interfaces': [{'interface': public_text(item.get('interface'), 32), 'address': address(item.get('address'))}
                         for item in health.get('addresses', [])[:8] if isinstance(item, dict)],
          'transport': 'USB' if any(c == 'usb' for c, _, _, _ in candidates) else 'Wi-Fi' if any(c == 'wifi' for c, _, _, _ in candidates)
          else 'SSH' if live else 'unknown'}
  preview = {k: v for k, v in status.get('preview_metrics', {}).items() if k in (
    'fps', 'cpu_percent', 'preview_latency_ms', 'frames', 'errors', 'target_fps') and finite(v)} if isinstance(status.get('preview_metrics'), dict) else {}
  for key in ('hud_bytes', 'hud_send_ms', 'camera_tx_latency_ms'):
    if finite(status.get(key)):
      preview[key] = status[key]
  return {'stages': stages, 'health': info, 'recent': recent, 'preview': preview,
          'capability_negotiated': bool(negotiation), 'camera_tx_recent': camera_tx, 'hud_tx_recent': hud_tx}


def diagnostic_summary(value):
  labels = {'jetson': 'JETSON', 'usb': 'JETSON USB', 'jetlink': 'JETLINK', 'camera': 'CAMERA', 'inference': 'INFERENCE', 'hud': 'HUD DATA'}
  lines = [f"{labels[stage['id']]}: {stage['state']}" + (f" ({public_text(stage['detail'])})" if stage.get('detail') else '')
           for stage in value['stages']]
  lines += [f"CAMERA TX: {'OK' if value['camera_tx_recent'] else 'WAITING' if value['connected'] else 'DISCONNECTED'}",
            f"HUD TX: {'OK' if value['hud_tx_recent'] else 'WAITING' if value['connected'] else 'DISCONNECTED'}"]
  for key, label in (('heartbeat_age_s', 'HEARTBEAT AGE'), ('camera_frame_age_s', 'CAMERA FRAME AGE'), ('inference_age_s', 'INFERENCE AGE')):
    delta = value['recent'][key]
    lines.append(f'{label}: {delta:.2f}s' if delta is not None else f'{label}: unknown')
  health = value['health']
  temp = health['temperature_c']
  lines += [f'JETSON TEMP: {temp:.1f}C' if temp is not None else 'JETSON TEMP: unknown',
            f"THERMAL: {health['thermal']} | STORAGE: {health['storage']}",
            f"JETSON IP: {value['status'].get('ip') or 'unknown'} | LINK: {health['transport']}",
            f"JETSON VERSION: {health['version']}", f"LAST ERROR: {public_text(value['status'].get('last_error')) or 'none'}"]
  return '\n'.join(lines)
