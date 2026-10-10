"""NEXO renderer on the inference link, with actual pinned server coverage."""
import importlib.util
import json
import os
from pathlib import Path
from collections import deque
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

from openpilot.cereal import log
from openpilot.selfdrive.modeld.jetlink import display, protocol as P
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec
from openpilot.selfdrive.carrot.server.services import jetson
from openpilot.tools.jetson import hud, native_host, manage
from openpilot.tools.jetson.state import atomic_json, read_fresh
from openpilot.tools.jetson.transport.base import Message


def snapshot():
  return {'version': 1, 'sent': 100., 'session': 'a' * 32, 'events': {}, 'params': {}, 'cameras': {},
          'mono': {}, 'received': {}, 'valid': {}, 'alive': {}}


@pytest.fixture
def session(monkeypatch, tmp_path):
  monkeypatch.setattr(native_host, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(native_host, 'RUNTIME', tmp_path)
  monkeypatch.setattr(native_host.time, 'monotonic', lambda: 1000.)
  calls = []
  class Base:
    def __init__(self):
      self.last_seq = 1
      self.telemetry = SimpleNamespace(health=None, read=lambda: {'loaded': 'model'})
    def handle(self, msg):
      calls.append(msg)
    def _send_json(self, *args):
      calls.append(args)
    def close(self):
      calls.append('closed')
  return native_host.session_class(Base)(), calls


def test_native_hud_replay_malformed_and_disconnect_expire(session):
  owner, calls = session
  value = snapshot()
  owner.handle(Message(P.Msg.HUD, 2, 0, memoryview(json.dumps(value).encode())))
  received = read_fresh(native_host.SNAPSHOT, now=1000.)
  assert received['updated'] == 1000. and received['session'] == 'a' * 32
  value['sent'] = 101.
  owner.handle(Message(P.Msg.HUD, 2, 0, memoryview(json.dumps(value).encode())))
  assert read_fresh(native_host.SNAPSHOT, now=1000.)['sent'] == 100.
  owner.handle(Message(P.Msg.HUD, 3, 0, memoryview(b'{"events":{"sendcan":"x"}}')))
  assert read_fresh(native_host.SNAPSHOT, now=1000.)['sent'] == 100.
  owner.handle(Message(P.Msg.INFER_REQ, 4, 0, memoryview(b'model')))
  assert calls[-1].msg_type == P.Msg.INFER_REQ
  assert read_fresh(native_host.SNAPSHOT, .5, now=1001.) is None
  owner.close()
  assert not native_host.SNAPSHOT.exists()


def test_handoff_hello_clears_old_snapshot_and_negotiates_nexo(session):
  owner, calls = session
  atomic_json(native_host.SNAPSHOT, {'updated': 1000.})
  owner.handle(Message(P.Msg.HELLO_REQ, 1, 0, memoryview(b'{}')))
  assert not native_host.SNAPSHOT.exists()
  owner._send_json(P.Msg.HELLO_RESP, 1, {'protocol': 2})
  assert calls[-1][2][display.CAPABILITY] is True


def test_hud_connection_requires_recent_renderer_evidence(session):
  owner, _ = session
  assert owner.telemetry.read()['carrot_hud_connected'] is False
  atomic_json(native_host.RUNTIME / 'hud-status.json', {'updated': 999.9})
  assert owner.telemetry.read()['carrot_hud_connected'] is True
  atomic_json(native_host.RUNTIME / 'hud-status.json', {'updated': 997.})
  assert owner.telemetry.read()['carrot_hud_connected'] is False


def test_navi_media_clock_translation_and_expiry():
  event = log.Event.new_message(logMonoTime=100_050_000_000)
  event.init('carrotNaviMedia')
  event.carrotNaviMedia.kind = 'image'
  value = snapshot()
  value['updated'] = 1000.
  received, = hud.shifted_media(log, [event], value, now=1000.1)
  assert received.logMonoTime == 1_000_050_000_000
  assert hud.shifted_media(log, [event], value, now=1003.) == []
  assert hud.shifted_media(log, [event], None, now=1000.1) == []


def test_native_7000_hud_tx_and_renderer_proof_are_independent(tmp_path, monkeypatch):
  monkeypatch.setattr(jetson, 'DISPLAY_STATUS', tmp_path / 'missing')
  monkeypatch.setattr(jetson, 'LINK_STATUS', (tmp_path / 'link.json',))
  monkeypatch.setattr(jetson, 'MODEL_STATUS', (tmp_path / 'model.json',))
  monkeypatch.setattr(jetson, 'UDC_ROOT', tmp_path / 'udcs')
  record = {'updated': 100., 'state': 'ready', 'telemetry_updated': 100., 'last_infer_monotonic': 99.95,
            'last_hud_tx_mono': 99.9, 'hud_enabled': True, 'capability_negotiated': True,
            'peer': {'carrot_hud_connected': False}}
  atomic_json(jetson.LINK_STATUS[0], record)
  result = jetson.snapshot({'channels': {}}, 100.)
  stage = next(s for s in result['stages'] if s['id'] == 'hud')
  assert result['hud_tx_recent'] and stage['detail'] == 'TX 완료'
  record['peer']['carrot_hud_connected'] = True
  atomic_json(jetson.LINK_STATUS[0], record)
  result = jetson.snapshot({'channels': {}}, 100.)
  assert next(s for s in result['stages'] if s['id'] == 'hud')['detail'] == '전송 완료 · Jetson HUD 표시 확인'
  record['last_hud_tx_mono'] = 98.
  record['peer']['carrot_hud_connected'] = True
  atomic_json(jetson.LINK_STATUS[0], record)
  result = jetson.snapshot({'channels': {}}, 100.)
  assert not result['hud_tx_recent']
  assert next(s for s in result['stages'] if s['id'] == 'hud')['detail'] == 'HUD 수신/표시 관측'
  assert not jetson.snapshot({'channels': {}}, 104.)['connected']


def test_native_management_selects_running_usb_owner(tmp_path):
  assert 'nexo-jetlink-server.service' in manage.SERVICES
  units = {'carrot-jetlink.service': {'WorkingDirectory': str(tmp_path / 'old'), 'ActiveState': 'inactive'},
           'nexo-jetlink-server.service': {'WorkingDirectory': str(tmp_path / 'native'), 'ActiveState': 'active'}}
  assert manage.root_for(units) == (tmp_path / 'native').resolve()


def test_actual_pinned_carrot_session_keeps_inference_output_with_hud(monkeypatch, tmp_path):
  root = Path(os.environ.get('CARROT_JETSON', str(Path.home() / 'carrot-jetson')))
  source = root / 'tools/jetlink/server.py'
  if not source.is_file():
    pytest.skip('pinned carrot-jetson checkout is required for server integration')
  spec = importlib.util.spec_from_file_location('nexo_pinned_server_test', source)
  upstream = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(upstream)
  from jetlink.spec import ModelSpec as ServerSpec
  from jetlink.queues import PolicyQueues
  from jetlink.transport.base import LinkTimeout
  monkeypatch.setattr(native_host, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(native_host, 'RUNTIME', tmp_path)
  contract = ModelSpec('a' * 64, 123, 4,
                       {'img': (1, 12, 2, 2), 'big_img': (1, 12, 2, 2), 'features_buffer': (1, 2, 4),
                        'desire_pulse': (1, 2, 2), 'traffic_convention': (1, 2), 'action_t': (1, 1)},
                       {'outputs': (1, 3)}, {'output': slice(0, 3)})
  server_spec = ServerSpec.from_dict(contract.to_dict())
  inputs = {key: np.zeros(shape, np.float32) for key, shape in server_spec.input_shapes.items()}
  def run():
    return {'outputs': np.array([inputs['img'].sum(), inputs['features_buffer'].sum(), 3.], np.float32)}
  loaded = SimpleNamespace(sha256=contract.sha256, spec=server_spec, queues=PolicyQueues(server_spec, np.float32),
                           host_inputs=inputs, engine=SimpleNamespace(run=run, last_gpu_us=1))
  host = SimpleNamespace(telemetry=SimpleNamespace(read=dict), lock=threading.RLock(), session=None,
                         loaded=loaded, backend=SimpleNamespace(describe=lambda: {'backend': 'numpy-test'}),
                         cache=SimpleNamespace(inventory=list), sleep_after=0., loaded_sha=lambda: contract.sha256,
                         status=lambda *a: {'state': 'ready'}, frame_stats=SimpleNamespace(record=lambda *a: None),
                         request=lambda *a: {'state': 'ready', 'spec': contract.to_dict()})
  replies = deque()
  def reply(kind, seq, parts, flags=0):
    raw = b''.join(memoryview(p).cast('B') for p in parts)
    replies.append(Message(kind, seq, flags, memoryview(raw)))
  def idle(timeout=None):
    time.sleep(.01)
    raise LinkTimeout('idle')
  server_transport = SimpleNamespace(send=reply, recv=idle)
  owner = native_host.session_class(upstream.CarrotSession)(server_transport, host)
  def send(kind, seq, parts, **kw):
    owner.handle(Message(kind, seq, 0, memoryview(b''.join(memoryview(p).cast('B') for p in parts))))
  client = JetlinkClient(SimpleNamespace(send=send, send_json=lambda k, s, value: send(k, s, [json.dumps(value).encode()]),
                                       recv=lambda timeout: replies.popleft(), close=lambda: None))
  try:
    assert client.hello()[display.CAPABILITY] is True
    client.ensure_engine(contract)
    images = np.ones(contract.warped_shape, np.uint8)
    packed = np.zeros(contract.packed_nelem, np.float32)
    before = client.infer(images, packed, 1, reset=True)
    value = snapshot()
    value['sent'] = time.monotonic()
    client.t.send(P.Msg.HUD, client._next_seq(), [display.snapshot_packet(json.dumps(value).encode(), 'a' * 32)])
    assert native_host.SNAPSHOT.is_file()
    # Bad display content consumes only its own sequence; model queues/results survive.
    client.t.send(P.Msg.HUD, client._next_seq(), [b'bad display'])
    after = client.infer(images, packed, 2, reset=True, want_state=True)
    np.testing.assert_array_equal(before, after)
    assert client.last_state['carrot_hud_connected'] is False
    assert not replies
  finally:
    owner.close()
