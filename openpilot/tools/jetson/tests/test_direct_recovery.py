"""Actual direct YOLO loop, with codec/GPU/network replaced by test doubles."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


def test_direct_loop_resyncs_comma_reboot_and_cleans_up_on_yolo_failure(monkeypatch, tmp_path):
  import openpilot.cereal
  from openpilot.tools.jetson import stream
  path = Path(__file__).resolve().parents[2] / 'camerastream/orin_yolo_direct.py'
  packets, sockets, decoders = [], [], []
  def socket_object():
    result = SimpleNamespace(closed=0, setsockopt=lambda *args: None, sendto=lambda *args: None)
    result.close = lambda: setattr(result, 'closed', result.closed + 1)
    sockets.append(result)
    return result
  def event(number, keyframe):
    video = SimpleNamespace(idx=SimpleNamespace(encodeId=number, flags=8 if keyframe else 0), header=b'h', data=b'd')
    return SimpleNamespace(which=lambda: 'roadEncodeData', roadEncodeData=video)
  events = iter([event(1, True), event(2, False), event(0, False), event(1, True), event(2, False)])
  messaging = ModuleType('openpilot.cereal.messaging')
  messaging.reset_context = lambda: None
  messaging.sub_sock = lambda *args, **kwargs: socket_object()
  messaging.recv_one = lambda sock: next(events)
  monkeypatch.setitem(sys.modules, messaging.__name__, messaging)
  monkeypatch.setattr(openpilot.cereal, 'messaging', messaging, raising=False)
  monkeypatch.setattr(stream.socket, 'gethostbyname', lambda host: host)
  av = ModuleType('av')
  frame = SimpleNamespace(to_ndarray=lambda **kwargs: SimpleNamespace(shape=(10, 20, 3)))
  class Decoder:
    def decode(self, raw):
      packets.append(raw)
      return [] if raw == b'h' else [frame]
  def decoder(*args):
    value = Decoder()
    decoders.append(value)
    return value
  av.CodecContext = SimpleNamespace(create=decoder)
  av.packet = SimpleNamespace(Packet=lambda raw: raw)
  monkeypatch.setitem(sys.modules, 'av', av)
  yolo = ModuleType('ultralytics')
  inferences = []
  class Model:
    def __init__(self, path):
      pass
    def predict(self, **kwargs):
      inferences.append(kwargs)
      if len(inferences) == 4:
        raise RuntimeError('synthetic GPU failure')
      return [SimpleNamespace(boxes=None)]
  yolo.YOLO = Model
  monkeypatch.setitem(sys.modules, 'ultralytics', yolo)
  spec = importlib.util.spec_from_file_location('tested_direct', path)
  direct = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(direct)
  monkeypatch.setattr(direct.socket, 'socket', lambda *args: socket_object())
  model = tmp_path / 'model.pt'
  model.touch()
  status = tmp_path / 'status.json'
  monkeypatch.setattr(sys, 'argv', ['orin_yolo_direct.py', '192.0.2.1', '--model', str(model),
                                  '--skip', '1', '--status-file', str(status)])
  with pytest.raises(RuntimeError, match='synthetic GPU failure'):
    direct.main()
  assert len(decoders) == 2 and packets.count(b'h') == 2
  assert len(inferences) == 4  # The reboot's dependent packet was skipped.
  assert all(sock.closed == 1 for sock in sockets)
  result = json.loads(status.read_text())
  assert result['state'] == 'error' and not result['yolo_recent']
  assert 'synthetic GPU failure' in result['last_error']
