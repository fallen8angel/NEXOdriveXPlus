"""Bounded, read-only evidence around NEXO's existing camera selection loops."""
from collections import deque
import time


class CameraGapTrace:
  def __init__(self, emit):
    self.emit = emit
    self.received = deque(maxlen=16)
    self.previous = None
    self.receive_count = 0
    self.next_emit = 0.

  def begin(self):
    self.received.clear()
    self.receive_count = 0

  def receive(self, client, camera):
    started = time.monotonic()
    buf = client.recv()  # Exactly one original receive; no resync or policy here.
    try:
      now = time.monotonic()
      self.receive_count += 1
      self.received.append({'camera': camera, 'frame_id': client.frame_id if buf is not None else None,
                            'sof': client.timestamp_sof if buf is not None else None,
                            'eof': client.timestamp_eof if buf is not None else None,
                            'received_mono': now, 'wait_ms': round((now - started) * 1000, 3)})
    except Exception:
      pass
    return buf

  def record(self, frame_id, dropped, model, *, prepare_only=False, **timings):
    # Trace/log failures must never change inference or frame-drop policy.
    try:
      current = {'frame_id': frame_id, 'received': list(self.received), 'receive_count': self.receive_count,
                 'backend': type(model).__name__, 'jetson_active': getattr(model, 'active', False),
                 'jetson_ready': getattr(model, 'ready', False),
                 'jetson_state': getattr(model, 'link_state', 'local'),
                 'jetson_error': str(getattr(model, 'error', ''))[:300], 'prepare_only': prepare_only,
                 'timings_ms': {k: round(v, 3) for k, v in timings.items()}}
      now = time.monotonic()
      previous_id = self.previous['frame_id'] if self.previous else None
      # First attachment and counter resets are not evidence of missing IDs.
      if dropped and previous_id is not None and frame_id > previous_id + 1 and now >= self.next_emit:
        self.next_emit = now + 1.
        self.emit('modelCameraGap', mono_time=now, dropped=dropped,
                  missing={'first': previous_id + 1, 'last': frame_id - 1, 'count': frame_id - previous_id - 1},
                  previous=self.previous, current=current)
      self.previous = current
    except Exception:
      # Retain the current cycle even when the logger raises.
      if 'current' in locals():
        self.previous = current
