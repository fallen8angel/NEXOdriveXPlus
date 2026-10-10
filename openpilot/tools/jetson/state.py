"""Bounded display telemetry and session state; no cereal/Params dependencies."""
import json
import math
import os
import re
from pathlib import Path
import time

RUNTIME = Path('/dev/shm/nexo-jetson')
STATUS = RUNTIME / 'status.json'
SNAPSHOT = RUNTIME / 'hud.json'
STALE = 2.0


def finite(value):
  return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def public_text(value, limit=240):
  text = str(value or '')[:limit]
  return '[민감 정보 숨김]' if re.search(r'password|passwd|psk|secret|token|authorization|ssid', text, re.I) else text


def public_telemetry(value, depth=0):
  """Known diagnostic fields only; never forward private provisioning data."""
  if not isinstance(value, dict):
    return {}
  result = {}
  for key in ('protocol', 'carrot_host', 'backend', 'engine_state', 'loaded'):
    item = value.get(key)
    if isinstance(item, (str, int)) and not isinstance(item, bool):
      result[key] = public_text(item, 128) if isinstance(item, str) else item
  for key in ('carrot_hud_v1', 'carrot_navi_v1', 'carrot_wifi_v1', 'carrot_hud_connected'):
    if isinstance(value.get(key), bool):
      result[key] = value[key]
  if depth < 1 and isinstance(value.get('telemetry'), dict):
    result['telemetry'] = public_telemetry(value['telemetry'], depth + 1)
  health = value.get('carrot_health')
  if isinstance(health, dict):
    safe = {key: health[key] for key in ('age_s', 'temp_c') if finite(health.get(key))}
    for key in ('severity', 'reason', 'storage_mode'):
      if isinstance(health.get(key), str):
        safe[key] = public_text(health[key])
    safe['addresses'] = [{'address': public_text(item.get('address'), 64), 'interface': public_text(item.get('interface'), 32)}
                         for item in health.get('addresses', [])[:8] if isinstance(item, dict)] if isinstance(health.get('addresses'), list) else []
    result['carrot_health'] = safe
  return result


def decode_json(raw, limit=65536):
  if not 0 < len(raw) <= limit:
    raise ValueError('display JSON exceeds budget')
  value = json.loads(raw, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
  if not isinstance(value, dict):
    raise ValueError('expected display object')
  return value


def atomic_json(path, value):
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
  try:
    tmp.write_text(json.dumps(value, allow_nan=False, separators=(',', ':')), encoding='utf-8')
    os.replace(tmp, path)
  finally:
    tmp.unlink(missing_ok=True)


def read_fresh(path, age=STALE, now=None, limit=1024 * 1024):
  now = time.monotonic() if now is None else now
  try:
    with Path(path).open('rb') as f:
      value = decode_json(f.read(limit + 1), limit)
    updated = value.get('updated')
    if finite(updated) and 0 <= now - updated < age:
      return value
  except (OSError, ValueError, TypeError, RecursionError):
    pass
  return None


class Peer:
  """One connection, one nonce, strictly increasing heartbeat sequence numbers.

  Payload traffic cannot extend the heartbeat lease. No remote clock is trusted
  to decide connectivity. A reboot requires a new transport connection.
  """
  def __init__(self, role):
    self.role = role
    self.session = None
    self.sequence = -1
    self.received = None
    self.value = {}

  def accept(self, raw, sequence, now):
    value = decode_json(raw, 4096)
    session = value.get('session')
    if value.get('role') != self.role or not isinstance(session, str) or len(session) != 32:
      raise ValueError('unexpected display peer')
    if self.session is not None and self.session != session:
      raise ValueError('peer rebooted; reopen transport')
    if sequence <= self.sequence:
      return False
    self.session, self.sequence, self.received = session, sequence, now
    self.value = value
    return True

  def alive(self, now):
    return self.received is not None and 0 <= now - self.received < STALE


def display_status(peer, now):
  connected = peer.alive(now)
  source = peer.value if connected else {}
  temp = source.get('temperature_c')
  return {
    'magic': 'NEXO_JETSON_STATUS', 'version': 5,
    'updated': now, 'usb_connected': connected, 'comma_tcp': False,
    'transport': 'usb',
    'service_active': connected, 'ip': str(source.get('ip', ''))[:64],
    'temperature_c': temp if finite(temp) and -40 <= temp <= 150 else None,
    'yolo_recent': source.get('yolo_recent') is True,
    'hud_connected': source.get('hud_connected') is True,
    'last_receive_monotonic': peer.received,
    'receive_age_s': max(0, now - peer.received) if peer.received is not None else None,
  }


class VideoGate:
  """A changed source or a dropped HEVC packet always waits for a keyframe."""
  def __init__(self):
    self.reset()

  def reset(self):
    self.last_id = None
    self.ready = False

  def accept(self, encode_id, keyframe, header):
    if self.last_id is not None and encode_id != self.last_id + 1:
      self.ready = False
    self.last_id = encode_id
    if keyframe and header:
      self.ready = True
    return self.ready
