"""Read-only native inference status and independent USB/Wi-Fi badge leases."""
import math
import json
from pathlib import Path
import time

JETLINK_ENABLED = Path('/data/nexo_jetlink_enabled')
JETLINK_STATUS = Path('/dev/shm/nexo-jetlink.json')
JETLINK_MODEL_STATUS = Path('/dev/shm/nexo-jetlink-model.json')


def _recent(stamp, now):
  if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
    return False
  try:
    return math.isfinite(stamp) and 0 <= now - stamp < 2.5
  except OverflowError:
    return False


def _record(path):
  try:
    with path.open('rb') as source:
      raw = source.read(65537)
    if len(raw) <= 65536:
      value = json.loads(raw)
      if isinstance(value, dict):
        return value
  except (OSError, ValueError, RecursionError):
    pass
  return {}


def native_presentation(link, model, now):
  """Shared UI/web interpretation. A progress badge never proves connection."""
  if not _recent(link.get('updated'), now):
    return None
  if (link.get('error') or (_recent(model.get('updated'), now) and model.get('error'))
      or link.get('state') in ('retrying', 'conflict')):
    label = {'transport': 'USB ERROR', 'model': '모델 ERROR', 'session': '재연결 ERROR'}.get(link.get('stage'), 'ERROR')
    return f'JETSON {label}', 'error'
  state = link.get('state')
  if state == 'ready':
    if not any(_recent(link.get(key), now) for key in ('telemetry_updated', 'last_infer_monotonic')):
      return None
    active = (model.get('active') is True and _recent(model.get('updated'), now)
              and bool(link.get('sha256')) and model.get('sha256') == link['sha256'])
    return ('JETSON', 'active') if active else ('JETSON READY', 'ready')
  if state == 'loading':
    return 'JETSON 모델 준비 중', 'loading'
  if state in ('connecting', 'waiting_model_contract'):
    return ('JETSON 통신 준비' if link.get('usb_checked') else 'JETSON 연결 대기'), 'loading'
  return None


class NativeJetlinkStatus:
  """Read-only badge for verified NEXO inference, inspired by Carrot status.

  An admin-UP interface or a saved ready file is insufficient: require a fresh
  successful peer exchange. Polling is bounded and never imports modeld/Params.
  """
  def __init__(self):
    self.next_check = 0.
    self.badge = None
    self.evidence = None

  def update(self, now=None):
    now = time.monotonic() if now is None else now
    # Even a cached badge expires against its original peer timestamp.
    if self.badge and not _recent(self.evidence, now):
      self.badge = None
    if now < self.next_check:
      return self.badge
    self.next_check = now + .25
    self.badge = None
    try:
      enabled = JETLINK_ENABLED.exists()
    except OSError:
      enabled = False
    if not enabled:
      return None
    link = _record(JETLINK_STATUS)
    model = _record(JETLINK_MODEL_STATUS)
    self.badge = native_presentation(link, model, now)
    self.evidence = link.get('updated')
    if self.badge and self.badge[1] in ('ready', 'active'):
      evidence = [stamp for key in ('telemetry_updated', 'last_infer_monotonic')
                  if _recent(stamp := link.get(key), now)]
      self.evidence = min(link['updated'], max(evidence))
    return self.badge


class JetsonConnectivity:
  def __init__(self):
    self.seen = {}

  def update(self, payload, now):
    if not isinstance(payload, dict) or payload.get('magic') != 'NEXO_JETSON_STATUS':
      return
    # A disconnected USB owner must not clear a still-live legacy Wi-Fi beacon.
    transport = payload.get('transport', 'wifi')
    if transport not in ('usb', 'wifi'):
      return
    key = 'usb_connected' if transport == 'usb' else 'comma_tcp'
    self.seen[transport] = now if payload.get(key) is True else None

  def connected(self, now):
    return any(stamp is not None and math.isfinite(stamp) and 0 <= now - stamp < 2.5 for stamp in self.seen.values())
