import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.tools.jetson import host, vehicle, yolo
from openpilot.tools.jetson.transport.base import LinkError, Message
from openpilot.tools.jetson.transport.protocol import Msg

ROOT = Path(__file__).resolve().parents[4]


class Messaging:
  def __init__(self):
    self.messages = []
    self.subscriptions = []

  def pub_sock(self, name):
    assert name == 'roadEncodeData'
    return SimpleNamespace(send=self.messages.append)

  def sub_sock(self, name, **kwargs):
    self.subscriptions.append((name, kwargs))
    return SimpleNamespace(receive=lambda **kwargs: None)

  @staticmethod
  def log_from_bytes(raw):
    with log.Event.from_bytes(raw) as event:
      return event.as_builder()


def encoded(number, keyframe):
  event = log.Event.new_message()
  video = event.init('roadEncodeData')
  video.idx.encodeId = number
  video.idx.flags = 8 if keyframe else 0
  video.header = b'header' if keyframe else b''
  video.data = b'compressed'
  return event.to_bytes()


def test_video_source_exclusivity_and_reconnect_keyframe(monkeypatch, tmp_path):
  monkeypatch.setattr(host, 'RUNTIME', tmp_path)
  messaging = Messaging()
  source = host.VideoSource(messaging, '192.0.2.1', True)
  source.select(False)
  assert source.wifi is not None
  old_epoch = source.epoch
  source.publish(encoded(1, False))
  assert not messaging.messages
  source.publish(encoded(2, True))
  assert len(messaging.messages) == 1
  source.select(True)
  assert source.wifi is None
  assert source.epoch != old_epoch
  source.publish(encoded(3, False))
  assert len(messaging.messages) == 1
  source.publish(encoded(4, True))
  assert len(messaging.messages) == 2
  source.select(True)
  assert source.wifi is None
  assert len(messaging.subscriptions) == 1
  source.select(False)
  assert len(messaging.subscriptions) == 2
  assert source.wifi is not None


def test_video_disabled_has_no_network_subscription(monkeypatch, tmp_path):
  monkeypatch.setattr(host, 'RUNTIME', tmp_path)
  messaging = Messaging()
  source = host.VideoSource(messaging, '192.0.2.1', False)
  source.select(False)
  source.select(True)
  assert not messaging.subscriptions
  source.publish(encoded(1, True))
  assert not messaging.messages


def test_usb_only_needs_no_guessed_wifi_address(monkeypatch, tmp_path):
  monkeypatch.setattr(host, 'RUNTIME', tmp_path)
  messaging = Messaging()
  source = host.VideoSource(messaging, None, True)
  source.select(False)
  assert source.source == 'disconnected'
  assert not messaging.subscriptions
  source.select(True)
  source.publish(encoded(1, True))
  assert len(messaging.messages) == 1
  assert host.local_ip(None) == ''


def test_vehicle_rejects_any_non_heartbeat_input(monkeypatch):
  statuses = []
  monkeypatch.setattr(vehicle, 'local_status', lambda peer, sock: statuses.append(peer))
  transport = SimpleNamespace(send=lambda *args, **kwargs: None,
                              recv=lambda *args: Message(Msg.ROAD_VIDEO, 1, 0, memoryview(b'not allowed')))
  with pytest.raises(LinkError, match='unexpected host message'):
    vehicle.session(transport, lambda: True, None, None)
  assert not statuses[-1].alive(100)


def test_vehicle_works_without_video_or_hud(monkeypatch):
  import time
  statuses = []
  monkeypatch.setattr(vehicle, 'local_status', lambda peer, sock: statuses.append(peer))
  nonce = 'f' * 32
  raw = json.dumps({'role': 'jetson', 'session': nonce, 'video': False, 'hud_connected': False}).encode()
  transport = SimpleNamespace(send=lambda *args, **kwargs: None,
                              recv=lambda *args: Message(Msg.HEARTBEAT, 1, 0, memoryview(raw)))
  polls = iter([True, False])
  flags = []
  publisher = SimpleNamespace(snapshot=lambda hud: flags.append(('hud', hud)),
                              media=lambda video: flags.append(('video', video)) or [])
  vehicle.session(transport, lambda: next(polls), publisher, None)
  assert flags == [('hud', False), ('video', False)]
  assert not statuses[-1].alive(time.monotonic())


def test_manager_gate_disabled_by_default_and_does_not_replace_bridge():
  tree = ast.parse((ROOT / 'openpilot/system/manager/process_config.py').read_text(encoding='utf-8'))
  gate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'nexo_jetson_usb')
  gate.returns = None
  for arg in gate.args.args:
    arg.annotation = None
  namespace = {'TICI': True}
  exec(compile(ast.Module(body=[gate], type_ignores=[]), '<manager-gate>', 'exec'), namespace)
  params = SimpleNamespace(get_bool=lambda key: False)
  assert not namespace['nexo_jetson_usb'](True, params, None)
  params.get_bool = lambda key: key == 'NexoJetsonUsb'
  assert namespace['nexo_jetson_usb'](False, params, None)
  namespace['TICI'] = False
  assert not namespace['nexo_jetson_usb'](True, params, None)
  names = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Name) and n.func.id in ('PythonProcess', 'NativeProcess', 'DaemonProcess')
           and n.args and isinstance(n.args[0], ast.Constant)]
  assert names.count('nexo_jetson_usb') == names.count('nexo_jetson_bridge') == 1
  assert len(names) == len(set(names))
  assert '{"NexoJetsonUsb", {PERSISTENT, BOOL, "0"}}' in (ROOT / 'openpilot/common/params_keys.h').read_text()


def test_yolo_interface_is_finite_bounded_and_display_only(monkeypatch, tmp_path):
  monkeypatch.setattr(yolo, 'RUNTIME', tmp_path)
  good = {'conf': .8, 'name': 'car', 'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}
  payload = {'magic': 'NEXO_JETSON_YOLO', 'width': 640, 'height': 480, 'encode_id': 10,
             'objects': [good, dict(good, conf=float('nan')), dict(good, x2=1000)]}
  yolo.publish_objects(payload, 20)
  value = json.loads((tmp_path / 'yolo.json').read_text())
  assert value['display_only'] is True
  assert value['objects'] == [good]
  payload['width'] = float('inf')
  with pytest.raises(ValueError):
    yolo.publish_objects(payload, 21)
