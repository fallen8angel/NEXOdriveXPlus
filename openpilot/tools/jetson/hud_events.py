"""Bounded renderer lifecycle evidence, independent of graphics and messaging."""
import os
import threading
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json, public_text

_lock = threading.Lock()
_stage = ''


def record(stage, error=None, reason=None, service=False):
  global _stage
  with _lock:
    if error is None and stage == _stage:
      return
    _stage = stage
    value = {'updated': time.monotonic(), 'pid': os.getpid(), 'stage': stage}
    if error is not None:
      value.update(reason=reason, error_type=type(error).__name__,
                   detail=public_text(str(error)), error_time_unix=time.time())
    try:
      atomic_json(RUNTIME / ('hud-service-error.json' if service else 'hud-stage.json'), value)
    except OSError:
      pass


class PanelFailure(RuntimeError):
  def __init__(self, error, stage):
    self.hud_reason = 'usb_open_failed' if stage == 'usb_open' else 'usb_write_failed'
    current, seen = error, set()
    while current is not None and id(current) not in seen:
      seen.add(id(current))
      code = getattr(current, 'errno', None)
      text = str(current).lower()
      if code in (1, 13):
        self.hud_reason = 'usb_permission'
        break
      if code == 16 or 'display is busy' in text:
        self.hud_reason = 'panel_busy'
        break
      if code in (19, 108) or 'disconnected' in text or 'device not found' in text:
        self.hud_reason = 'usb_disconnected'
        break
      current = current.__cause__ or current.__context__
    super().__init__(f'{stage}: {type(error).__name__}: {public_text(str(error))}')
