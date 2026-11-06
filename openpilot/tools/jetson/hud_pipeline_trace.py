"""Bounded, read-only timing evidence for Jetson HUD blackouts.

This does not alter vehicle data, steering, encoding, panel ownership, or
expiry policy. Monotonic times are strictly Jetson-local. No frame pixels or
driver/camera content are stored.
"""
from collections import deque
import math
import threading
import time


def _safe_number(value):
  return round(float(value), 4) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


class HudPipelineTrace:
  def __init__(self, max_events=48, panel_gap_threshold_s=.15):
    # A 10 FPS panel should receive an AU about every 100ms. The old 400ms
    # alarm missed the 100-300ms blackouts reported on 2026-11-06. This is
    # instrumentation only: no timeout/restart, encoder or USB writes change.
    self._lock = threading.Lock()
    self._events = deque(maxlen=max_events)
    self._panel_gap_threshold_s = panel_gap_threshold_s
    self._mode = None
    self._reason = None
    self._last_panel = None
    self._last_encoded = None
    self._last_input = None
    self._panel_gap_count = 0
    self._panel_gap_max_s = 0.
    self._encoder_expired_count = 0
    self._last_encoder_expired = None
    self._adaptive_guard_uses = 0
    self._encoder_sent_count = 0
    self._waiting_started = None
    self._waiting_episodes = 0
    self._waiting_total_s = 0.
    self._waiting_max_s = 0.

  def _event(self, now, kind, **details):
    event = {'at_mono': round(now, 4), 'kind': kind}
    for key, value in details.items():
      if isinstance(value, str):
        event[key] = value[:64]
      else:
        number = _safe_number(value)
        if number is not None:
          event[key] = number
    self._events.append(event)

  def event(self, kind, *, now=None, **details):
    now = time.monotonic() if now is None else now
    with self._lock:
      self._event(now, kind, **details)

  def input(self, mode, *, reason=None, age_s=None, now=None):
    now = time.monotonic() if now is None else now
    reason = reason if mode == 'waiting_data' else None
    with self._lock:
      self._last_input = now
      if self._mode != 'waiting_data' and mode == 'waiting_data':
        self._waiting_started = now
        self._waiting_episodes += 1
      elif self._mode == 'waiting_data' and mode != 'waiting_data':
        if self._waiting_started is not None and now >= self._waiting_started:
          duration = now - self._waiting_started
          self._waiting_total_s += duration
          self._waiting_max_s = max(self._waiting_max_s, duration)
          self._event(now, 'waiting_end', duration_s=duration)
        self._waiting_started = None
      if (mode, reason) != (self._mode, self._reason):
        self._mode, self._reason = mode, reason
        self._event(now, 'input_mode', mode=mode, reason=reason or '', source_age_s=age_s)

  def encoder_reserve_s(self, *, now=None, baseline=.10, recovery=.15, hold_s=4.):
    """Only after an IDR expiry, protect a little more of the next few frames.

    Unlike permanently increasing the guard (which causes more visible dark
    waiting screens), this expires after a bounded monotonic interval. The
    same strict 500ms source lease and whole-IDR expiry gate remain active.
    """
    now = time.monotonic() if now is None else now
    with self._lock:
      recent = (self._last_encoder_expired is not None
                and 0 <= now - self._last_encoder_expired < hold_s)
      if recent:
        self._adaptive_guard_uses += 1
    return max(baseline, recovery) if recent else baseline

  def encoded(self, *, expired, mode, lease_remaining_s=None, now=None):
    now = time.monotonic() if now is None else now
    with self._lock:
      if expired:
        self._encoder_expired_count += 1
        self._last_encoder_expired = now
        self._event(now, 'encoder_expired', mode=mode, lease_remaining_s=lease_remaining_s)
      else:
        self._encoder_sent_count += 1
        self._last_encoded = now

  def panel(self, *, now=None):
    now = time.monotonic() if now is None else now
    with self._lock:
      if self._last_panel is not None:
        gap = now - self._last_panel
        if gap > self._panel_gap_threshold_s:
          self._panel_gap_count += 1
          self._panel_gap_max_s = max(self._panel_gap_max_s, gap)
          self._event(now, 'panel_write_gap', duration_s=gap)
      self._last_panel = now

  def snapshot(self, *, now=None):
    now = time.monotonic() if now is None else now
    with self._lock:
      age = lambda timestamp: max(0., now - timestamp) if timestamp is not None else None
      return {
        'version': 1,
        'updated': now,
        'input_mode': self._mode,
        'input_reason': self._reason,
        'input_age_s': _safe_number(age(self._last_input)),
        'encoded_age_s': _safe_number(age(self._last_encoded)),
        'panel_write_age_s': _safe_number(age(self._last_panel)),
        'panel_gap_count': self._panel_gap_count,
        'panel_gap_threshold_s': self._panel_gap_threshold_s,
        'panel_gap_max_s': _safe_number(self._panel_gap_max_s),
        'encoder_sent_count': self._encoder_sent_count,
        'encoder_expired_count': self._encoder_expired_count,
        'encoder_last_expiry_age_s': _safe_number(age(self._last_encoder_expired)),
        'adaptive_guard_uses': self._adaptive_guard_uses,
        'waiting_episodes': self._waiting_episodes,
        'waiting_completed_total_s': _safe_number(self._waiting_total_s),
        'waiting_max_s': _safe_number(self._waiting_max_s),
        'events': list(self._events),
      }


HUD_TRACE = HudPipelineTrace()
