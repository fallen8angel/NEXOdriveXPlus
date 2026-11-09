"""Bounded Jetson USB command history, retained across renderer restarts in RAM."""
import os
import sys
import threading
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite, read_fresh

_lock = threading.Lock()


def history(role):
  rows = (read_fresh(RUNTIME / f'panel-events-{role}.json', float('inf'), limit=16384) or {}).get('events')
  if not isinstance(rows, list):
    return []
  return [entry for entry in rows[-16:] if isinstance(entry, dict) and finite(entry.get('at_mono'))]


def record(role, kind, **details):
  now = time.monotonic()
  event = dict(at_mono=now, jetson_unix=time.time(), pid=os.getpid(), role=role, kind=kind, **details)
  with _lock:
    try:
      events = history(role)[-15:] + [event]
      atomic_json(RUNTIME / f'panel-events-{role}.json', dict(updated=now, events=events))
    except OSError:
      pass


def observed_panel(original, role):
  class Panel(original):
    def _connect_device(self):
      super()._connect_device()
      record(role, 'usb_opened', bus=getattr(self.dev, 'bus', None), address=getattr(self.dev, 'address', None),
             product=self.dev_pid)

    def _initialize_device(self):
      record(role, 'panel_init_requested', reason='usb_open')
      return super()._initialize_device()

    def _reset_and_reconnect(self):
      if self.dev is not None:
        record(role, 'usb_reset_requested', reason='usb_error_recovery', error_type=type(sys.exc_info()[1]).__name__)
      return super()._reset_and_reconnect()

    def _mark_disconnected(self):
      record(role, 'usb_io_disconnect', reason='libusb_error')
      return super()._mark_disconnected()

    def start_h264_stream(self, requested_chunk_size=0):
      record(role, 'h264_start_requested', reason='encoder_start')
      return super().start_h264_stream(requested_chunk_size)

    def _send_command(self, command_id, name, fields=None, **kwargs):
      # Observe only lifecycle/settings writes. Status polls and frame chunks
      # stay on their existing hot path. A command attempt is not optical proof.
      if name in ('brightness', 'brightness-off', 'orientation', 'video-setup-13', 'stop-stream'):
        record(role, 'usb_command_requested', command=name, value=(fields or {}).get(8),
               reason='panel_close' if name == 'brightness-off' else 'encoder_close' if name == 'stop-stream'
               else 'display_setting')
      return super()._send_command(command_id, name, fields, **kwargs)
  return Panel
