"""Shared USB inference/HUD ordering, budgets, capabilities and worker expiry."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.jetlink import display, protocol as P
from openpilot.selfdrive.modeld.jetlink.link import REQUEST, REPLY
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec
from openpilot.tools.jetson.transport.base import LinkTimeout


def snapshot():
  return {'version': 1, 'sent': 100., 'events': {}, 'params': {}, 'cameras': {},
          'mono': {}, 'received': {}, 'valid': {}, 'alive': {}}


def spec():
  return ModelSpec('a' * 64, 123, 4,
                   {'img': (1, 12, 2, 2), 'features_buffer': (1, 2, 4), 'desire_pulse': (1, 2, 2),
                    'traffic_convention': (1, 2), 'action_t': (1, 1)}, {'outputs': (1, 3)}, {'output': slice(0, 3)})


def publisher(tmp_path):
  obj = display.DisplayPublisher.__new__(display.DisplayPublisher)
  obj.path = tmp_path / 'snapshot.packet'
  obj.last_sent = obj.next_hud = 0.
  obj.hud_connected = None
  obj.status = {}
  obj.process = SimpleNamespace(poll=lambda: None)
  obj.media_socket = obj.media_process = None
  return obj


def test_bounded_packet_preserves_nexo_parking_settings_and_side_camera():
  value = snapshot()
  value['events'] = {'can': 'parking', 'carState': 'vehicle', 'navRoute': 'x' * 180000, 'modelV2': 'model'}
  value['params'] = {'ClusterHudSideCameraWidth': 'NDA='}
  value['cameras'] = {'driver': {'jpeg': 'preview'}}
  received = json.loads(display.snapshot_packet(json.dumps(value).encode(), 'a' * 32))
  assert 'navRoute' not in received['events']
  assert received['events']['can'] == 'parking'
  assert received['params'] == value['params'] and received['cameras'] == value['cameras']
  assert received['session'] == 'a' * 32


def test_oversized_core_is_skipped_instead_of_sending_unbounded_packet():
  value = snapshot()
  value['events']['carState'] = 'x' * (display.HUD_LIMIT + 1)
  with pytest.raises(ValueError, match='core snapshot'):
    display.snapshot_packet(json.dumps(value).encode(), 'a' * 32)


def test_large_inline_guidance_image_does_not_freeze_vehicle_display():
  value = snapshot()
  value['events']['carState'] = 'vehicle'
  value['params']['CarrotNaviImage'] = 'x' * 100000
  received = json.loads(display.snapshot_packet(json.dumps(value).encode(), 'a' * 32))
  assert received['events']['carState'] == 'vehicle'
  assert 'CarrotNaviImage' not in received['params']


@pytest.mark.parametrize('bad', ['sendcan', 'pandaStates'])
def test_publisher_cannot_forward_other_vehicle_services(bad):
  value = snapshot()
  value['events'][bad] = 'not-a-display-service'
  with pytest.raises(ValueError):
    display.snapshot_packet(json.dumps(value).encode(), 'a' * 32)


def test_missing_duplicate_stale_future_oversize_worker_data_expires(tmp_path):
  obj = publisher(tmp_path)
  assert obj.packet(100.) is None
  obj.path.write_bytes(display.HEADER.pack(99.9) + b'latest')
  assert obj.packet(100.) == b'latest'
  assert obj.packet(100.) is None
  for stamp, raw in [(99., b'old'), (101., b'future'), (99.99, b'x' * (display.HUD_LIMIT + 1))]:
    obj.path.write_bytes(display.HEADER.pack(stamp) + raw)
    assert obj.packet(100.) is None
  obj.process = SimpleNamespace(poll=lambda: 1)
  assert obj.packet(100.) is None
  assert 'inference continues' in obj.status['display_error']


def test_shared_sequence_and_bounded_tail_sends_hud_and_media(tmp_path):
  obj = publisher(tmp_path)
  obj.path.write_bytes(display.HEADER.pack(99.9) + b'latest')
  packets = iter([b'navi-1', b'navi-2', b'navi-3'])
  obj.media_packet = lambda: next(packets)
  sent = []
  sequence = iter(range(3, 10))
  client = SimpleNamespace(last_state={}, _next_seq=lambda: next(sequence),
                           t=SimpleNamespace(send=lambda kind, seq, parts, **kw: sent.append((kind, seq, parts, kw))))
  obj.send(client, clock=lambda: 100.)
  assert [s[0] for s in sent] == [P.Msg.HUD, P.Msg.NAVI_MEDIA, P.Msg.NAVI_MEDIA]
  assert [s[1] for s in sent] == [3, 4, 5]
  assert all(0 < s[3]['timeout'] <= display.TAIL_BUDGET + 1e-10 for s in sent)
  assert obj.status['last_hud_tx_mono'] == 100.
  assert next(packets) == b'navi-3'


def test_tail_stops_admitting_packets_after_budget(tmp_path):
  obj = publisher(tmp_path)
  obj.packet = lambda now: None
  obj.media_packet = lambda: pytest.fail('budget expired; must not drain producer')
  clocks = iter([100., 100.02, 100.02])
  obj.send(SimpleNamespace(last_state={}), clock=lambda: next(clocks))


def test_shared_usb_write_failure_is_not_hidden(tmp_path):
  obj = publisher(tmp_path)
  obj.path.write_bytes(display.HEADER.pack(99.9) + b'latest')
  def fail(*args, **kwargs):
    raise LinkTimeout('partially written shared USB stream')
  client = SimpleNamespace(last_state={}, _next_seq=lambda: 4, t=SimpleNamespace(send=fail))
  with pytest.raises(LinkTimeout):
    obj.send(client, clock=lambda: 100.)


def test_late_hud_and_replug_publish_actual_connection_edges(tmp_path):
  obj = publisher(tmp_path)
  obj.packet = lambda now: None
  obj.media_packet = lambda: None
  client = SimpleNamespace(last_state={})
  # The producer may be writing its next snapshot when the owner updates the
  # feedback marker; their temporary files must not replace one another.
  obj.path.with_suffix('.tmp').write_bytes(b'worker snapshot in progress')
  obj.send(client, clock=lambda: 100.)
  assert obj.path.with_suffix('.tmp').read_bytes() == b'worker snapshot in progress'
  assert obj.path.with_suffix('.connected').read_bytes() == b'0'
  client.last_state = {'carrot_hud_connected': True}
  obj.send(client, clock=lambda: 101.)
  assert obj.path.with_suffix('.connected').read_bytes() == b'1'
  client.last_state = {'telemetry': {'carrot_hud_connected': False}}
  obj.send(client, clock=lambda: 102.)
  assert obj.path.with_suffix('.connected').read_bytes() == b'0'
  client.last_state['telemetry']['carrot_hud_connected'] = True
  obj.send(client, clock=lambda: 103.)
  assert obj.path.with_suffix('.connected').read_bytes() == b'1'


@pytest.mark.parametrize('telemetry', [None, [], 'unavailable'])
def test_optional_bad_telemetry_does_not_break_inference_owner(tmp_path, telemetry):
  obj = publisher(tmp_path)
  obj.packet = lambda now: None
  obj.media_packet = lambda: None
  obj.send(SimpleNamespace(last_state=telemetry), clock=lambda: 100.)
  assert obj.path.with_suffix('.connected').read_bytes() == b'0'


def test_media_send_retries_same_atomic_fragment_on_backpressure(monkeypatch):
  sent, attempts = [], []
  def send(packet):
    attempts.append(packet)
    if len(attempts) == 1:
      raise TimeoutError
    sent.append(packet)
    return len(packet)
  monkeypatch.setattr(display.time, 'monotonic', lambda: 100.)
  sock = SimpleNamespace(settimeout=lambda t: None, send=send)
  assert display.send_event(sock, bytes(display.CHUNK + 2), 17, 5)
  assert attempts[0] == attempts[1]
  assert [display.MEDIA_HEADER.unpack_from(p)[2] for p in sent] == [0, display.CHUNK]
  assert not display.send_event(sock, b'', 17, 6)
  assert not display.send_event(sock, bytes(display.MAX_MEDIA + 1), 17, 7)


@pytest.fixture
def daemon():
  # Execute the real functions without importing the Linux Params extension.
  # All inference bytes, parsing and ordering below use production functions.
  source = Path(__file__).resolve().parents[1] / 'jetlink/daemon.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
  scope = {'np': np, 'time': SimpleNamespace(monotonic=lambda: 100.), 'json': json,
           'REQUEST': REQUEST, 'REPLY': REPLY, 'ModelSpec': ModelSpec, 'CAPABILITY': display.CAPABILITY}
  exec(compile(tree, str(source), 'exec'), scope)
  return scope


def test_inference_reply_precedes_all_display_work(daemon):
  contract = spec()
  request = (REQUEST.pack(20, 0, 10) + np.zeros(contract.warped_shape, np.uint8).tobytes()
             + np.zeros(contract.packed_nelem, np.float32).tobytes())
  order = []
  turns = iter([True, True, False, False])
  class Connection:
    def __enter__(self):
      return self
    def __exit__(self, *args):
      return False
    def settimeout(self, timeout):
      pass
  connection = Connection()
  daemon['enabled'] = lambda: next(turns)
  daemon['publish'] = lambda *a, **k: None
  daemon['PacketReader'] = lambda size: SimpleNamespace(receive=lambda sock: request)
  def send_parts(sock, *parts):
    order.append('modeld reply' if len(parts) == 2 else 'contract')
    if len(parts) == 2:
      assert REPLY.unpack(parts[0])[0] == 20
      np.testing.assert_array_equal(parts[1], [1., 2., 3.])
  daemon['send_parts'] = send_parts
  def infer(*a, **k):
    order.append('inference')
    return np.array([1., 2., 3.], np.float32)
  client = SimpleNamespace(last_state={}, last_timings=(1, 2, 3), infer=infer)
  producer = SimpleNamespace(send=lambda client: order.append('HUD/navigation'))
  daemon['_serve_modeld'](SimpleNamespace(accept=lambda: (connection, None)), client, contract, producer, {})
  assert order == ['contract', 'inference', 'modeld reply', 'HUD/navigation']


@pytest.mark.parametrize('peer', [None, {}, {'carrot_hud_v1': True}, {display.CAPABILITY: True}])
def test_stock_or_unnegotiated_server_still_infers_without_starting_hud(daemon, peer):
  daemon['DisplayPublisher'] = lambda **kw: pytest.fail('NEXO HUD capability was not negotiated')
  calls = []
  daemon['_serve_modeld'] = lambda *args: calls.append(args)
  daemon['serve_modeld'](None, None, None, peer)
  assert calls[0][3] is None


def test_worker_start_failure_is_optional_and_cleanup_runs(daemon):
  calls = []
  def fail(**kw):
    raise OSError('missing optional worker')
  daemon['DisplayPublisher'] = fail
  daemon['cloudlog'] = SimpleNamespace(warning=lambda *a: None)
  daemon['_serve_modeld'] = lambda *args: calls.append(args)
  daemon['serve_modeld'](None, None, None, {display.CAPABILITY: True, 'carrot_hud_v1': True})
  assert calls[0][3] is None and 'OSError' in calls[0][4]['display_error']


def test_worker_stops_when_inference_session_ends(daemon):
  closed = []
  producer = SimpleNamespace(status={}, close=lambda: closed.append(True))
  daemon['DisplayPublisher'] = lambda **kw: producer
  def fail(*args):
    raise ConnectionError('unplugged')
  daemon['_serve_modeld'] = fail
  with pytest.raises(ConnectionError):
    daemon['serve_modeld'](None, None, None, {display.CAPABILITY: True, 'carrot_hud_v1': True})
  assert closed == [True]
