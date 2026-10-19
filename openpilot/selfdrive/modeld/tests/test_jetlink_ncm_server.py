"""Integration with the actual pinned Carrot Session/queues, fake GPU only.

Set CARROT_JETSON to a checkout of d0dc96fa32410403dfb2fefff3e2909c11ec109b.
Linux interface enforcement is separately tested on target hardware.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec, NATIVE_MODEL
from openpilot.tools.jetson import native_host, ncm_server


@pytest.fixture(scope='module')
def pinned():
  root = Path(os.environ.get('CARROT_JETSON', '/missing'))
  source = root / 'tools/jetlink/server.py'
  if not source.is_file():
    pytest.skip('set CARROT_JETSON to the pinned checkout for integration')
  spec = importlib.util.spec_from_file_location('nexo_ncm_pinned_server_test', source)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.fixture(scope='module')
def contract(pinned):
  pytest.importorskip('onnx')
  from jetlink.spec import spec_from_onnx
  return spec_from_onnx(str(NATIVE_MODEL))


def test_real_native_onnx_contract_matches_vehicle_and_wire(contract):
  assert contract.sha256 == 'f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5'
  local = ModelSpec.from_dict(contract.to_dict())
  assert local.to_dict() == contract.to_dict()
  assert local.output_slices['pad'] == slice(-2, None)
  # Reproduce the old allocation failure, then exercise the corrected contract.
  with pytest.raises(TypeError):
    max(int(v.stop) for v in local.output_slices.values())
  assert local.output_nelem == contract.output_nelem == 2576
  assert local.output_nbytes == 10304
  assert local.warped_nbytes + local.packed_nbytes + P.INFER_REQ_SIZE == contract.infer_req_nbytes == 395320


def test_pinned_protocol_values_and_padding_match(pinned):
  from jetlink import protocol as upstream
  assert P.HEADER_SIZE == upstream.HEADER_SIZE == 32
  assert P.VERSION == upstream.VERSION == 2
  for name in ('HELLO_REQ', 'ENGINE_REQ', 'INFER_REQ', 'INFER_RESP', 'STATE_REQ', 'PING', 'SHUTDOWN_REQ'):
    assert int(getattr(P.Msg, name)) == int(getattr(upstream.Msg, name))
  assert P.pack_header(P.Msg.INFER_REQ, 42, 395320, P.Flag.RESET_QUEUES) == upstream.pack_header(
    upstream.Msg.INFER_REQ, 42, 395320, upstream.Flag.RESET_QUEUES)


@pytest.mark.parametrize('kind', [P.Msg.UPLOAD_CHUNK, P.Msg.UPLOAD_DONE, P.Msg.SHUTDOWN_REQ, 0x4002])
def test_ncm_forbids_upload_poweroff_and_wifi(pinned, contract, kind):
  from jetlink.transport.base import LinkError, Message
  class Base:
    def handle(self, msg):
      pytest.fail('disallowed command reached upstream')
  owner = ncm_server.session_class(Base, contract)()
  with pytest.raises(LinkError, match='unsupported'):
    owner.handle(Message(kind, 1, 0, memoryview(b'{}')))


@pytest.mark.parametrize('field,value', [('sha256', 'a' * 64), ('nbytes', 1), ('frame_skip', 1)])
def test_server_rejects_wrong_model_request(pinned, contract, field, value):
  from jetlink.transport.base import LinkError, Message
  class Base:
    def handle(self, msg):
      pytest.fail('mismatched engine request reached upstream')
  request = {k: getattr(contract, k) for k in ('sha256', 'nbytes', 'frame_skip')}
  request[field] = value
  owner = ncm_server.session_class(Base, contract)()
  with pytest.raises(LinkError, match='model'):
    owner.handle(Message(P.Msg.ENGINE_REQ, 1, 0, memoryview(json.dumps(request).encode())))


def test_server_rejects_idle_and_oversize_without_buffer_growth(pinned):
  from jetlink.transport.base import LinkError
  for oversized in (False, True):
    left, right = socket.socketpair()
    transport = ncm_server.transport_class()(right)
    try:
      if oversized:
        left.sendall(P.pack_header(P.Msg.PING, 1, (1 << 20) + 2))
      else:
        transport.last_message = time.monotonic() - 61
      with pytest.raises(LinkError):
        transport.recv(.1)
      assert len(transport.rx.buf) == 1 << 20
    finally:
      left.close()
      transport.close()


@pytest.mark.parametrize('attempt', range(3))
def test_real_tcp_pinned_server_inference_hud_reconnect(pinned, contract, monkeypatch, tmp_path, attempt):
  from jetlink.queues import PolicyQueues
  from jetlink.transport.base import LinkError
  from host_reader import ReadAheadTransport
  monkeypatch.setattr(native_host, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(native_host, 'RUNTIME', tmp_path)
  local = ModelSpec.from_dict(contract.to_dict())
  inputs = {key: np.zeros(shape, np.float32) for key, shape in contract.input_shapes.items()}
  output = np.arange(contract.output_nelem, dtype=np.float32)
  loaded = SimpleNamespace(sha256=contract.sha256, spec=contract, queues=PolicyQueues(contract, np.float32),
                           host_inputs=inputs, engine=SimpleNamespace(run=lambda: {'outputs': output}, last_gpu_us=1))
  host = SimpleNamespace(telemetry=SimpleNamespace(read=dict), lock=threading.RLock(), session=None,
                         loaded=loaded, backend=SimpleNamespace(describe=lambda: {'backend': 'numpy-test'}),
                         cache=SimpleNamespace(inventory=list), sleep_after=0., loaded_sha=lambda: contract.sha256,
                         status=lambda *a: {'state': 'ready'}, frame_stats=SimpleNamespace(record=lambda *a: None),
                         request=lambda *a: {'state': 'ready', 'spec': contract.to_dict()})
  listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  listener.bind(('127.0.0.1', 0))
  listener.listen(1)
  peer = socket.create_connection(listener.getsockname(), timeout=2)
  accepted, _ = listener.accept()
  listener.close()
  server_transport = ncm_server.transport_class()(accepted)
  owner = ncm_server.session_class(native_host.session_class(pinned.CarrotSession), contract)(server_transport, host)
  # Exercise the same simultaneous read-ahead/send path even on Windows.
  if not isinstance(owner.t, ReadAheadTransport):
    owner.t = ReadAheadTransport(server_transport)
  errors = []
  def serve():
    try:
      owner.serve_forever()
    except LinkError:
      pass
    except Exception as exc:
      errors.append(exc)
    finally:
      owner.close()
      server_transport.close()
  worker = threading.Thread(target=serve)
  worker.start()
  client = JetlinkClient(JetlinkNcmTcpTransport(peer), deadline=2)
  try:
    assert client.hello()['nexo_hud_v1'] is True
    client.ensure_engine(local)
    images = np.ones(local.warped_shape, np.uint8)
    packed = np.zeros(local.packed_nelem, np.float32)
    np.testing.assert_array_equal(client.infer(images, packed, 1, reset=True), output)
    snapshot = {'version': 1, 'sent': time.monotonic(), 'events': {}, 'params': {}, 'cameras': {},
                'mono': {}, 'received': {}, 'valid': {}, 'alive': {}, 'session': 'a' * 32}
    client.t.send_json(P.Msg.HUD, client._next_seq(), snapshot)
    np.testing.assert_array_equal(client.infer(images, packed, 2, reset=True, want_state=True), output)
    assert native_host.SNAPSHOT.is_file()
    # A rejected model never leaves the client ready to infer.
    bad = ModelSpec.from_dict({**local.to_dict(), 'sha256': 'b' * 64})
    with pytest.raises((OSError, RuntimeError)):
      client.ensure_engine(bad, timeout=.5)
    assert client.spec is None
  finally:
    client.close()
    worker.join(3)
    server_transport.close()
  assert not worker.is_alive() and not errors
  assert not native_host.SNAPSHOT.exists()


def test_ncm_listener_rejects_other_peer_and_reuses_listener(pinned, monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
  bound, checks = [], []
  class Connection:
    closed = False
    def close(self):
      self.closed = True
  rejected, first, second = Connection(), Connection(), Connection()
  arrivals = iter([(rejected, ('192.168.100.99', 1)),
                   (first, (ncm.COMMA_ADDR, 2)), (second, (ncm.COMMA_ADDR, 3))])
  class Listener:
    def setsockopt(self, *args):
      pass
    def bind(self, address):
      assert address == (ncm.JETSON_ADDR, ncm.PORT)
    def listen(self, backlog):
      assert backlog == 1
    def settimeout(self, timeout):
      assert timeout == 1
    def accept(self):
      return next(arrivals)
    def close(self):
      pass
  monkeypatch.setattr(ncm.socket, 'socket', lambda *a: Listener())
  monkeypatch.setattr(ncm, 'require_usb_route', lambda *a, **kw: checks.append((a, kw)))
  monkeypatch.setattr(ncm, 'bind_device', lambda sock, iface: bound.append(iface))
  monkeypatch.setattr(ncm_server, 'transport_class', lambda: lambda conn: SimpleNamespace(conn=conn))
  opener = ncm_server.tcp_opener('enxVerified')(SimpleNamespace(host=ncm.JETSON_ADDR, port=ncm.PORT))
  assert opener() is None and rejected.closed
  assert opener().conn is first
  assert opener().conn is second
  assert bound == ['enxVerified'] * 3 and len(checks) == 3
  opener.close()


def test_invalid_tcp_header_closes_instead_of_bulk_drain(pinned):
  from jetlink.transport.base import LinkError
  left, right = socket.socketpair()
  transport = ncm_server.transport_class()(right)
  try:
    left.sendall(b'bad!' + P.pack_header(P.Msg.PING, 1, 0)[4:])
    with pytest.raises(LinkError):
      transport.recv(.5)
    assert transport._desynced
    transport.drain(5)
    assert right.fileno() == -1
  finally:
    left.close()
    transport.close()
