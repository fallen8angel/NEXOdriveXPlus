"""Pinned Carrot inference server plus the existing NEXO HUD snapshot consumer.

Run on Jetson only. Carrot owns USB, TensorRT, ordered navigation fragments
and read-ahead; the optional NEXO snapshot adapter never publishes controls.
"""
from __future__ import annotations

import importlib.util
import argparse
import os
from pathlib import Path
import time

from openpilot.selfdrive.modeld.jetlink.display import CAPABILITY, HUD_LIMIT
from openpilot.selfdrive.modeld.jetlink.protocol import Msg
from openpilot.tools.jetson.snapshot import validate_snapshot
from openpilot.tools.jetson.state import RUNTIME, SNAPSHOT, atomic_json, decode_json, read_fresh
from openpilot.common.stability import memory_observer
from openpilot.tools.jetson.performance_history import PerformanceHistoryMonitor
from openpilot.selfdrive.modeld.jetlink.startup import RESIDENT_CAPABILITY


def resident_backend(base):
  """The live server may load existing artifacts; only Offroad tools build."""
  class LoadOnlyBackend(base):
    def build(self, *args, **kwargs):
      raise RuntimeError('Live host compilation disabled; use explicit Offroad prepare_engine')
  return LoadOnlyBackend


def session_class(base):
  class NexoSession(base):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      self.telemetry = NexoTelemetry(self.telemetry)
      SNAPSHOT.unlink(missing_ok=True)

    def _send_json(self, msg_type, seq, obj, flags=0):
      if msg_type == Msg.HELLO_RESP:
        obj = {**obj, CAPABILITY: True, RESIDENT_CAPABILITY: True}
      elif msg_type == Msg.STATE_RESP and callable(getattr(self.host, 'snapshot', None)):
        obj = {**obj, 'nexo_engine_preparation': self.host.snapshot()}
      return super()._send_json(msg_type, seq, obj, flags)

    def on_engine_req(self, msg):
      # The live connection only binds an already resident exact engine. Do
      # not delegate to EngineHost.request(), which can compile/swap models.
      from jetlink.server.session import Request
      value = decode_json(bytes(msg.payload), 4096)
      if (type(value.get('nbytes')) is not int or value['nbytes'] <= 0
          or type(value.get('frame_skip')) is not int or value['frame_skip'] <= 0
          or not isinstance(value.get('sha256'), str)):
        raise ValueError('Invalid resident engine request')
      request = Request(value['sha256'], value['nbytes'], value['frame_skip'])
      with self.host.lock:
        loaded = self.host.loaded
        ready = (loaded is not None and loaded.sha256 == request.sha256
                 and loaded.spec.nbytes == request.nbytes and loaded.spec.frame_skip == request.frame_skip)
        if ready:
          self.request = request
          self.host.session = self
          response = {'state': 'ready', 'sha256': request.sha256, 'spec': loaded.spec.to_dict()}
        else:
          self.request = None
          response = {'state': 'not_resident', 'sha256': request.sha256,
                      'detail': 'Use explicit Offroad engine preparation; live sessions never build or switch models'}
      self._send_json(Msg.ENGINE_RESP, msg.seq, response)

    def handle(self, msg):
      if msg.msg_type in (Msg.UPLOAD_CHUNK, Msg.UPLOAD_DONE):
        return self._error(msg.seq, 'offroad_preparation_required', 'NEXO live sessions do not upload or build models')
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
    # Cached procfs observation; no per-inference scan or native allocator action.
    return {**self.original.read(), 'carrot_hud_connected': read_fresh(RUNTIME / 'hud-status.json', 2) is not None,
            'nexo_memory': memory_observer.sample(),
            # Counters describe this boot's last session, never liveness. A
            # healthy renderer need not rewrite them on every frame.
            'nexo_hud_recovery': read_fresh(RUNTIME / 'hud-recovery.json', float('inf'), limit=4096) or {}}


def main():
  if not Path('/etc/nv_tegra_release').is_file():
    raise RuntimeError('native inference/HUD host must run on Jetson')
  if os.environ.get('NEXO_JETLINK_DISPLAY_ONLY') == '1':
    from openpilot.tools.jetson.ncm_display import host_main
    with PerformanceHistoryMonitor():
      return host_main()
  root = Path(os.environ.get('CARROT_JETSON', str(Path.home() / 'carrot-jetson'))).resolve()
  path = root / 'tools/jetlink/server.py'
  if not path.is_file():
    raise RuntimeError('install the pinned carrot-jetson server before starting NEXO host')
  spec = importlib.util.spec_from_file_location('nexo_carrot_server', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  from openpilot.tools.jetson.onnx_inputs import install_carrot_patch
  install_carrot_patch()
  from openpilot.tools.jetson.trt_diagnostics import install as install_trt_profile
  backend = resident_backend(install_trt_profile())
  def select_resident(name='auto', device='auto'):
    if name not in ('auto', 'trt'):
      raise ValueError('NEXO live host requires its verified TensorRT cache')
    return backend(device)
  module.server.select = select_resident
  module.server.Session = session_class(module.CarrotSession)
  parser = argparse.ArgumentParser(add_help=False)
  parser.add_argument('--transport', default='tcp')
  args, _ = parser.parse_known_args()
  mode = os.environ.get('NEXO_JETLINK_TRANSPORT', 'usb')
  openers = []
  if mode == 'ncm-tcp' and args.transport == 'tcp':
    from jetlink.spec import spec_from_onnx
    from openpilot.selfdrive.modeld.jetlink.spec import NATIVE_MODEL
    from openpilot.tools.jetson import ncm_server
    contract = spec_from_onnx(str(NATIVE_MODEL))
    expected = os.environ.get('NEXO_JETLINK_MODEL_SHA256', '')
    if contract.sha256 != expected:
      raise RuntimeError('NCM model SHA does not match the explicitly verified comma model')
    module.server.Session = ncm_server.session_class(module.server.Session, contract)
    create = ncm_server.tcp_opener(os.environ.get('NEXO_JETLINK_USB_IFACE', ''))
    def tracked_opener(args):
      opener = create(args)
      openers.append(opener)
      return opener
    module.server.OPENERS['tcp'] = tracked_opener
  elif mode != 'usb' or args.transport != 'usb':
    raise RuntimeError('use explicit NCM-TCP installation for TCP; default native host is USB only')
  RUNTIME.mkdir(parents=True, exist_ok=True)
  try:
    with PerformanceHistoryMonitor():
      return module.server.main()
  finally:
    for opener in openers:
      opener.close()


if __name__ == '__main__':
  raise SystemExit(main())
