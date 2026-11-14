"""Passive external-HUD status; never starts inference or selects a driving model.

All producer timestamps are on Comma's monotonic clock. Engine readiness and
synthetic GPU benchmarks are not evidence of completed live model inference.
"""
from __future__ import annotations

from pathlib import Path
import time

from openpilot.tools.jetson.state import RUNTIME, STATUS, finite, read_fresh

MODEL_STATUS = Path('/dev/shm/nexo-jetlink-model.json')
LINK_STATUS = Path('/dev/shm/nexo-jetlink.json')
DISPLAY_STATUS = RUNTIME / 'ncm-display.json'
MODEL_NAME = 'NEXO native driving_supercombo'
MODEL_LEASE_S = 2.25


def _recent(stamp, now):
  try:
    return finite(stamp) and 0 <= now - stamp < MODEL_LEASE_S
  except OverflowError:
    return False


def _milliseconds(value):
  try:
    return value if finite(value) and 0 <= value < 10000 else None
  except OverflowError:
    return None


def _read(path, now, limit=65536):
  try:
    return read_fresh(path, MODEL_LEASE_S, now=now, limit=limit) or {}
  except OverflowError:
    return {}


def status_on_comma(now=None):
  """Read bounded existing records only, with independent link/inference leases.

  Modeld already records a completed response's timestamp, count and timings.
  A refreshed status file or active flag cannot renew that response's lease.
  No enable marker is written, no engine requested, and no result is forwarded
  to modelV2, planning, steering, acceleration or braking.
  """
  now = time.monotonic() if now is None else now
  model = _read(MODEL_STATUS, now, limit=4096)
  link = _read(LINK_STATUS, now)
  display = _read(DISPLAY_STATUS, now)
  legacy = _read(STATUS, now)
  native_live = (link.get('state') == 'ready' and not link.get('error')
                 and any(_recent(link.get(key), now) for key in ('telemetry_updated', 'last_infer_monotonic')))
  display_live = (display.get('state') == 'connected' and display.get('hello_complete') is True
                  and _recent(display.get('last_peer_rx_mono'), now))
  legacy_live = legacy.get('usb_connected') is True and _recent(legacy.get('last_receive_monotonic'), now)
  connected = bool(native_live or display_live or legacy_live)
  engine_ready = bool(connected and (native_live or (legacy_live and legacy.get('model_ready') is True)))
  sha = link.get('sha256')
  valid_sha = isinstance(sha, str) and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)
  active = bool(native_live and _recent(link.get('last_infer_monotonic'), now)
                and model.get('active') is True and model.get('ready') is True
                and model.get('external_inference_verified') is True and not model.get('error')
                and model.get('model') == MODEL_NAME and valid_sha
                and model.get('sha256') == link['sha256']
                and type(model.get('completed_inferences')) is int and model['completed_inferences'] > 0
                and _recent(model.get('last_inference_at_mono'), now))
  result = {'connected': connected, 'engine_ready': engine_ready, 'inference_active': active}
  if active:
    peer_stamp = max(link[key] for key in ('telemetry_updated', 'last_infer_monotonic') if _recent(link.get(key), now))
    oldest = min(model['last_inference_at_mono'], model['updated'], link['updated'], peer_stamp, link['last_infer_monotonic'])
    result['inference_remaining_s'] = MODEL_LEASE_S - (now - oldest)
    # Execution is in microseconds, unlike the round trip; validate before conversion.
    raw_execution = model.get('jetson_execution_us')
    if type(raw_execution) in (int, float) and 0 <= raw_execution < 1e7:
      execution = _milliseconds(raw_execution / 1000)
      if execution is not None:
        result['inference_ms'] = execution
    rtt = _milliseconds(model.get('modeld_roundtrip_ms'))
    if rtt is not None:
      result['roundtrip_ms'] = rtt
  if display_live:
    rtt = _milliseconds(display.get('state_rtt_ms'))
    if rtt is not None:
      result['link_rtt_ms'] = rtt
  return result


def verified_on_comma(now=None):
  """Compatibility API: only a fresh completed native inference proves ON."""
  return status_on_comma(now)['inference_active']


def sanitize(value):
  """Optional metadata never invalidates a vehicle/gear/camera HUD frame."""
  if (not isinstance(value, dict)
      or any(type(value.get(key)) is not bool for key in ('connected', 'engine_ready', 'inference_active'))):
    return None
  result = {key: value[key] for key in ('connected', 'engine_ready', 'inference_active')}
  result['engine_ready'] &= result['connected']
  remaining = value.get('inference_remaining_s')
  valid_remaining = _milliseconds(remaining) is not None and 0 < remaining <= MODEL_LEASE_S
  result['inference_active'] &= result['engine_ready'] and valid_remaining
  if result['inference_active']:
    result['inference_remaining_s'] = remaining
    for key in ('inference_ms', 'roundtrip_ms'):
      if _milliseconds(value.get(key)) is not None:
        result[key] = value[key]
  if result['connected'] and _milliseconds(value.get('link_rtt_ms')) is not None:
    result['link_rtt_ms'] = value['link_rtt_ms']
  return result


def presentation(snapshot, now=None):
  """Renderer uses local receipt age, never compares Jetson and Comma clocks."""
  if not isinstance(snapshot, dict):
    return None
  now = time.monotonic() if now is None else now
  if not _recent(snapshot.get('updated'), now) or now - snapshot['updated'] >= .5:
    return None
  status = sanitize(snapshot.get('jetlink_ai'))
  if status is None:
    return None  # Old boolean-only snapshots cannot prove current inference.
  active = (status['inference_active']
            and now - snapshot['updated'] < status['inference_remaining_s'])
  label, color = ('JETSON AI ON', (65, 221, 146)) if active else (
    ('JETSON · AI 실험 대기', (236, 190, 84)) if status['engine_ready'] else
    ('JETSON 연결 · AI 대기', (151, 181, 209)) if status['connected'] else
    ('JETSON 연결 끊김', (164, 164, 164)))
  measurements = []
  if active:
    if 'inference_ms' in status:
      measurements.append(f"GPU {status['inference_ms']:.2f}ms")
    if 'roundtrip_ms' in status:
      measurements.append(f"추론 RTT {status['roundtrip_ms']:.1f}ms")
  if 'link_rtt_ms' in status and (not active or 'roundtrip_ms' not in status):
    measurements.append(f"통신 RTT {status['link_rtt_ms']:.1f}ms")
  return label, color, ' / '.join(measurements)


def visible_from_snapshot(snapshot):
  view = presentation(snapshot)
  return view is not None and view[0] == 'JETSON AI ON'


def install(renderer):
  """Overlay after the existing gear renderer; no panel/transport reconfiguration."""
  cls = renderer.ClusterUiRenderer
  if getattr(cls, '_nexo_jetlink_ai_badge_installed', False):
    return
  previous_render = cls.render

  def draw(self, state, signal_lights=None):
    result = previous_render(self, state, signal_lights)
    from openpilot.tools.jetson.snapshot import read_snapshot
    view = presentation(read_snapshot())
    if view is None:
      return result
    label, color, measurements = view
    rl = renderer.rl
    rl.rl_push_matrix()
    rl.rl_scalef(self.width / renderer.DESIGN_WIDTH,
                self.height / renderer.DESIGN_HEIGHT, 1.0)
    try:
      # P's hydrogen gauge starts below y=64; keep that existing title clear.
      parked = getattr(getattr(self, '_jetson_gears', None), 'mode', None) == 'park'
      x, y, width, height = renderer.DESIGN_WIDTH - 330.0, 8.0 if parked else 29.0, 293.0, 48.0 if parked else 64.0
      self._rounded_rect(x, y, width, height, 13.0, (12, 28, 35), color, 2.0)
      self._draw_text(label, x + 12.0, y + (16.0 if measurements else height * .5), 21.0, color)
      if measurements:
        self._draw_text(measurements, x + 12.0, y + height - 13.0, 13.0, (218, 226, 233))
    finally:
      rl.rl_pop_matrix()
    return result

  cls.render = draw
  cls._nexo_jetlink_ai_badge_installed = True
