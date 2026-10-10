"""Pinned Carrot inference server plus the existing NEXO HUD snapshot consumer.

Run on Jetson only. Carrot owns USB, TensorRT, ordered navigation fragments
and read-ahead; the optional NEXO snapshot adapter never publishes controls.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import time

from openpilot.selfdrive.modeld.jetlink.display import CAPABILITY, HUD_LIMIT
from openpilot.selfdrive.modeld.jetlink.protocol import Msg
from openpilot.tools.jetson.snapshot import validate_snapshot
from openpilot.tools.jetson.state import RUNTIME, SNAPSHOT, atomic_json, decode_json, read_fresh


def session_class(base):
  class NexoSession(base):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      self.telemetry = NexoTelemetry(self.telemetry)
      SNAPSHOT.unlink(missing_ok=True)

    def _send_json(self, msg_type, seq, obj, flags=0):
      if msg_type == Msg.HELLO_RESP:
        obj = {**obj, CAPABILITY: True}
      return super()._send_json(msg_type, seq, obj, flags)

    def handle(self, msg):
      if msg.msg_type == Msg.HELLO_REQ:
        SNAPSHOT.unlink(missing_ok=True)  # A client restart must expire the previous display generation.
      if msg.msg_type != Msg.HUD:
        return super().handle(msg)
      if msg.seq <= self.last_seq:
        return
      self.last_seq = msg.seq
      try:
        value = validate_snapshot(decode_json(bytes(msg.payload), HUD_LIMIT))
        session = value.get('session')
        if not isinstance(session, str) or len(session) != 32:
          return
        value['updated'] = time.monotonic()
        atomic_json(SNAPSHOT, value)
      except (OSError, ValueError, TypeError, RecursionError):
        pass  # A malformed/unavailable display does not invalidate inference.

    def close(self):
      try:
        super().close()
      finally:
        SNAPSHOT.unlink(missing_ok=True)

  return NexoSession


class NexoTelemetry:
  def __init__(self, original):
    self.original = original
    self.health = original.health

  def read(self):
    return {**self.original.read(), 'carrot_hud_connected': read_fresh(RUNTIME / 'hud-status.json', 2) is not None}


def main():
  if not Path('/etc/nv_tegra_release').is_file():
    raise RuntimeError('native inference/HUD host must run on Jetson')
  root = Path(os.environ.get('CARROT_JETSON', str(Path.home() / 'carrot-jetson'))).resolve()
  path = root / 'tools/jetlink/server.py'
  if not path.is_file():
    raise RuntimeError('install the pinned carrot-jetson server before starting NEXO host')
  spec = importlib.util.spec_from_file_location('nexo_carrot_server', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  module.server.Session = session_class(module.CarrotSession)
  RUNTIME.mkdir(parents=True, exist_ok=True)
  return module.server.main()


if __name__ == '__main__':
  raise SystemExit(main())
