"""Wire-level v2/v3 mock servers using native NEXO contracts, no accelerator."""
from dataclasses import replace
import json
import struct
import threading
import time

import numpy as np
import pytest

from openpilot.selfdrive.modeld.jetlink import protocol as V2
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.compat import ProtocolChoice, Reconnect, wire_protocol
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec
from openpilot.selfdrive.modeld.jetlink.transport import JetlinkTcpTransport, JetlinkFfsTransport, UnsafeCleanupError
from openpilot.tools.jetson.transport.base import LinkError, StreamTransport


def spec():
  return ModelSpec('a'*64, 123, 4, {'img': (1, 12, 2, 2), 'features_buffer': (1, 2, 4),
                   'desire_pulse': (1, 2, 3), 'traffic_convention': (1, 2), 'action_t': (1, 1)},
                   {'outputs': (1, 7)}, {'plan': slice(0, 3), 'hidden_state': slice(3, 7)})


class MemoryWire(StreamTransport):
  def __init__(self, protocol, raw=b''):
    super().__init__()
    self.wire_protocol = protocol
    self.raw = bytearray(raw)
    self.output = bytearray()

  def _write(self, bufs):
    raw = bytes(bufs[0][:17])
    self.output.extend(raw)
    return len(raw)

  def _read_into(self, dest, timeout):
    if not self.raw:
      raise LinkError('EOF')
    size = min(len(dest), len(self.raw), 7)
    dest[:size] = self.raw[:size]
    del self.raw[:size]
    return size

  def close(self):
    pass


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('size', [0, 480, 992, 16352, 19000])
def test_exact_headers_padding_and_fragmented_consecutive_reads(version, size):
  protocol = wire_protocol(version)
  sender = MemoryWire(protocol)
  sender.send(V2.Msg.PING, 10, [b'x'*size])
  sender.send(V2.Msg.PING, 11, [b'y'])
  header = struct.unpack_from('<IHHIIIQ4x', sender.output)
  assert header[:4] == (0x4B4E4C4A, version, 15, 10)
  assert bool(header[4] & 128) == ((32+size) % (1024 if version == 2 else 512) == 0)
  receiver = MemoryWire(protocol, sender.output)
  assert bytes(receiver.recv(.5).payload) == b'x'*size
  assert bytes(receiver.recv(.5).payload) == b'y'
  assert not receiver.raw and receiver.rx.available == 0


@pytest.mark.parametrize('version', [2, 3])
def test_mismatched_headers_latch_dead_stream(version):
  other = wire_protocol(5-version)
  receiver = MemoryWire(wire_protocol(version), other.pack_header(V2.Msg.PING, 1, 0))
  with pytest.raises(LinkError, match='protocol'):
    receiver.recv(.1)
  with pytest.raises(LinkError, match='desynced'):
    receiver.recv(.1)


class MockJetson:
  def __init__(self, version, *, mismatch=False, fault=None, client_version=None):
    client_sock, server_sock = MemorySocket(), MemorySocket()
    client_sock.peer, server_sock.peer = server_sock, client_sock
    self.client = JetlinkClient(JetlinkTcpTransport(client_sock, client_version or version), deadline=1.)
    self.server = JetlinkTcpTransport(server_sock, version)
    self.version, self.mismatch, self.fault = version, mismatch, fault
    self.seen, self.errors = [], []
    self.thread = threading.Thread(target=self.run)
    self.thread.start()

  def run(self):
    try:
      while True:
        msg = self.server.recv(2.)
        self.seen.append((msg.msg_type, bytes(msg.payload)))
        if msg.msg_type == V2.Msg.HELLO_REQ:
          self.server.send_json(V2.Msg.HELLO_RESP, msg.seq, {'protocol': self.version})
        elif msg.msg_type == V2.Msg.ENGINE_REQ:
          wanted = json.loads(bytes(msg.payload))
          assert wanted['sha256'] == spec().sha256
          contract = replace(spec(), sha256='b'*64) if self.mismatch else spec()
          self.server.send_json(V2.Msg.ENGINE_RESP, msg.seq, {'state': 'ready', 'spec': contract.to_dict()})
        elif msg.msg_type == V2.Msg.INFER_REQ:
          frame, flags = struct.unpack_from('<II', msg.payload)
          expected_size = 8 + spec().warped_nbytes + (spec().packed_nbytes if self.version == 2 else 24)
          assert len(msg.payload) == expected_size
          assert bool(flags & 4) == (self.version == 3)
          assert flags & 1  # Explicit recurrent reset survives both versions.
          output = np.arange(7, dtype=np.float32)
          if self.fault == 'compact':
            output = output[:3]
          if self.fault == 'nan':
            output[0] = np.nan
          payload = struct.pack('<IIIII', frame + (self.fault == 'frame'), 0, 1000, 200, 1500) + output.tobytes()
          if flags & 2:
            payload += b'{"temperature_c":42}'
          self.server.send(V2.Msg.INFER_RESP, msg.seq, [payload])
        elif msg.msg_type == V2.Msg.STATE_REQ:
          self.server.send_json(V2.Msg.STATE_RESP, msg.seq, {'ready': True})
        elif msg.msg_type in (V2.Msg.HUD, V2.Msg.NAVI_MEDIA):
          pass
    except LinkError:
      pass
    except Exception as exc:
      self.errors.append(exc)
    finally:
      self.server.close()

  def close(self):
    self.client.close()
    self.thread.join(3)
    assert not self.thread.is_alive()
    assert not self.errors


class MemorySocket:
  """Socket primitive double: exercise real framed transport without OS TCP."""
  def __init__(self):
    self.condition = threading.Condition()
    self.inbox = bytearray()
    self.closed = self.eof = False
    self.timeout = None

  def settimeout(self, timeout):
    self.timeout = timeout

  def send(self, buf):
    with self.peer.condition:
      if self.peer.closed:
        raise OSError('peer closed')
      size = min(len(buf), 31)
      self.peer.inbox.extend(buf[:size])
      self.peer.condition.notify_all()
      return size

  def recv_into(self, dest):
    deadline = None if self.timeout is None else time.monotonic() + self.timeout
    with self.condition:
      while not self.inbox and not self.eof:
        remaining = None if deadline is None else deadline-time.monotonic()
        if remaining is not None and remaining <= 0:
          raise TimeoutError()
        self.condition.wait(remaining)
      size = min(len(dest), len(self.inbox), 23)
      dest[:size] = self.inbox[:size]
      del self.inbox[:size]
      return size

  def close(self):
    self.closed = True
    with self.peer.condition:
      self.peer.eof = True
      self.peer.condition.notify_all()


@pytest.mark.parametrize('version', [2, 3])
def test_mock_hello_native_model_inference_hud_navigation_and_disconnect(version):
  mock = MockJetson(version)
  try:
    assert mock.client.hello()['protocol'] == version
    mock.client.ensure_engine(spec())
    output = mock.client.infer(np.zeros(spec().warped_shape, np.uint8), np.zeros(spec().packed_nelem, np.float32),
                               44, reset=True, want_state=True)
    np.testing.assert_array_equal(output, np.arange(7, dtype=np.float32))
    assert mock.client.last_state['temperature_c'] == 42
    assert mock.client.last_timings == (1000, 200, 1500)
    mock.client.t.send(V2.Msg.HUD, mock.client._next_seq(), [b'HUD'])
    mock.client.t.send(V2.Msg.NAVI_MEDIA, mock.client._next_seq(), [b'NAVI'])
    assert mock.client.state()['ready']
    assert [m[0] for m in mock.seen][-3:] == [V2.Msg.HUD, V2.Msg.NAVI_MEDIA, V2.Msg.STATE_REQ]
  finally:
    mock.close()


@pytest.mark.parametrize('version', [2, 3])
def test_model_sha_mismatch_is_never_ready(version):
  mock = MockJetson(version, mismatch=True)
  try:
    mock.client.hello()
    with pytest.raises(LinkError, match='contract'):
      mock.client.ensure_engine(spec())
    assert mock.client.spec is None
  finally:
    mock.close()


@pytest.mark.parametrize('fault', ['compact', 'nan', 'frame'])
def test_v3_invalid_outputs_abandon_session(fault):
  mock = MockJetson(3, fault=fault)
  try:
    mock.client.hello()
    mock.client.ensure_engine(spec())
    with pytest.raises(LinkError):
      mock.client.infer(np.zeros(spec().warped_shape, np.uint8), np.zeros(spec().packed_nelem, np.float32), 1, reset=True)
    assert mock.client.dead
  finally:
    mock.close()


def test_late_boot_reconnect_has_bounded_retries_and_keeps_successful_version():
  choice, retry = ProtocolChoice(), Reconnect()
  delays = []
  for _ in range(10):
    retry.attempt()
    choice.handshake_failed()
    delays.append(retry.failed('late boot / enumeration failure', 1.))
  assert delays == [2., 4., 8., 16., 30., 30., 30., 30., 30., 30.]
  assert retry.status()['reconnect_count'] == 9
  assert choice.version == 3
  choice.handshake_failed()
  assert choice.version == 2
  retry.ready_since = 2.
  assert retry.failed('disconnect after stable service', 33.) == 2.
  assert choice.version == 2  # Engine/inference failures do not negotiate.
  retry.attempt()
  mock = MockJetson(choice.version)
  try:
    assert mock.client.hello()['protocol'] == 2
  finally:
    mock.close()


@pytest.mark.parametrize('server_version',[2,3])
def test_automatic_selection_uses_fresh_stream_after_rejected_hello(server_version):
  choice=ProtocolChoice()
  for _ in range(2):
    mock=MockJetson(server_version,client_version=choice.version)
    try:
      try:
        peer=mock.client.hello(timeout=.5)
      except LinkError:
        choice.handshake_failed()
        continue
      assert peer['protocol']==choice.version==server_version
      break
    finally:
      mock.close()
  else:
    pytest.fail('neither fresh stream negotiated the deployed server')


@pytest.mark.parametrize('reader_alive,watchdog_alive,bound',[(True,False,''),(False,True,''),(False,False,'udc')])
def test_usb_cleanup_refuses_rebind_with_live_workers_or_controller(monkeypatch,tmp_path,reader_alive,watchdog_alive,bound):
  from types import SimpleNamespace
  from openpilot.tools.jetson.transport.ffs import FfsTransport
  transport=JetlinkFfsTransport.__new__(JetlinkFfsTransport)
  transport._reader=SimpleNamespace(is_alive=lambda:reader_alive)
  transport._write_guard=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:watchdog_alive))
  transport.gadget=str(tmp_path)
  (tmp_path/'UDC').write_text(bound)
  monkeypatch.setattr(FfsTransport,'close',lambda self:setattr(self,'_reader',None))
  with pytest.raises(UnsafeCleanupError):
    transport.close()
