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
  received_images = []
  original_infer = owner._infer
  def inspect_images(loaded, msg):
    received_images.append(bytes(msg.payload[P.INFER_REQ_SIZE:P.INFER_REQ_SIZE + local.warped_nbytes]))
    return original_infer(loaded, msg)
  owner._infer = inspect_images
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
    from openpilot.selfdrive.modeld.jetlink.check_ncm import inspect_peer
    observation = inspect_peer(client, contract.sha256)
    assert observation['model_sha_matches'] and not observation['inference_verified']
    assert not observation['eligible']  # The test uses a numpy backend, not real TensorRT.
    assert client.hello()['nexo_hud_v1'] is True
    client.ensure_engine(local)
    images = np.arange(local.warped_nbytes, dtype=np.uint8).reshape(local.warped_shape)
    packed = np.zeros(local.packed_nelem, np.float32)
    np.testing.assert_array_equal(client.infer(images, packed, 1, reset=True), output)
    snapshot = {'version': 1, 'sent': time.monotonic(), 'events': {}, 'params': {}, 'cameras': {},
                'mono': {}, 'received': {}, 'valid': {}, 'alive': {}, 'session': 'a' * 32}
    client.t.send_json(P.Msg.HUD, client._next_seq(), snapshot)
    np.testing.assert_array_equal(client.infer(images, packed, 2, reset=True, want_state=True), output)
    assert received_images == [images.tobytes(), images.tobytes()]
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
  monkeypatch.setattr(ncm.socket, 'if_nametoindex', lambda iface: 7)
  monkeypatch.setattr(ncm, 'require_usb_route', lambda *a, **kw: checks.append((a, kw)))
  monkeypatch.setattr(ncm, 'bind_device', lambda sock, iface: bound.append(iface))
  monkeypatch.setattr(ncm_server, 'transport_class', lambda: lambda conn: SimpleNamespace(conn=conn))
  opener = ncm_server.tcp_opener('enxVerified')(SimpleNamespace(host=ncm.JETSON_ADDR, port=ncm.PORT))
  assert opener() is None and rejected.closed
  assert opener().conn is first
  assert opener().conn is second
  assert bound == ['enxVerified'] * 3 and len(checks) == 3
  opener.close()


@pytest.mark.parametrize('initially_ready', [False, True])
def test_listener_recovers_missing_address_and_recreated_usb_interface(monkeypatch, caplog, initially_ready):
  from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
  index, provisioned = [7], [initially_ready]
  listeners, checks, pinned_devices = [], [], []
  def route(*args, **kwargs):
    checks.append(provisioned[0])
    if not provisioned[0]:
      raise ncm.LinkError('USB address not ready')
  def ifindex(iface):
    assert iface == 'enxVerified'
    if index[0] is None:
      raise OSError('No such device')
    return index[0]
  class Listener:
    def __init__(self):
      self.closed = False
      self.arrival = None
      listeners.append(self)
    def setsockopt(self, *args):
      pass
    def bind(self, address):
      assert address == (ncm.JETSON_ADDR, ncm.PORT)
    def listen(self, backlog):
      pass
    def settimeout(self, timeout):
      pass
    def accept(self):
      if self.arrival is not None:
        return self.arrival, (ncm.COMMA_ADDR, 1)
      raise TimeoutError
    def close(self):
      self.closed = True
  monkeypatch.setattr(ncm, 'require_usb_route', route)
  monkeypatch.setattr(ncm, 'bind_device', lambda sock, iface: pinned_devices.append(sock))
  monkeypatch.setattr(ncm.socket, 'if_nametoindex', ifindex)
  monkeypatch.setattr(ncm.socket, 'socket', lambda *a: Listener())
  monkeypatch.setattr(ncm_server, 'transport_class', lambda: lambda conn: SimpleNamespace(conn=conn))
  opener = ncm_server.tcp_opener('enxVerified')(SimpleNamespace(host=ncm.JETSON_ADDR, port=ncm.PORT))
  if not initially_ready:
    assert opener() is None and not listeners
    assert opener() is None and len(caplog.records) == 1  # no duplicate warning storm
  provisioned[0] = True
  assert opener() is None and len(listeners) == 1
  index[0] = None
  assert opener() is None and listeners[0].closed
  index[0] = 8
  assert opener() is None and len(listeners) == 2
  index[0] = 9  # detach/replug completely between polls, same interface name
  assert opener() is None and len(listeners) == 3 and listeners[1].closed
  connection = SimpleNamespace(close=lambda: pytest.fail('healthy connection closed'))
  listeners[-1].arrival = connection
  assert opener().conn is connection
  assert pinned_devices == [*listeners, connection]
  assert checks == ([] if initially_ready else [False, False]) + [True] * 4
  opener.close()
  assert all(listener.closed for listener in listeners)


def test_listener_closes_accepted_peer_after_route_loss(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
  calls, closed = [], []
  def route(*args, **kwargs):
    calls.append(True)
    if len(calls) == 2:
      raise ncm.LinkError('USB route disappeared')
  connection = SimpleNamespace(close=lambda: closed.append('peer'))
  listener = SimpleNamespace(setsockopt=lambda *a: None, bind=lambda *a: None, listen=lambda *a: None,
                             settimeout=lambda *a: None, accept=lambda: (connection, (ncm.COMMA_ADDR, 1)),
                             close=lambda: closed.append('listener'))
  monkeypatch.setattr(ncm, 'require_usb_route', route)
  monkeypatch.setattr(ncm, 'bind_device', lambda *a: None)
  monkeypatch.setattr(ncm.socket, 'if_nametoindex', lambda iface: 7)
  monkeypatch.setattr(ncm.socket, 'socket', lambda *a: listener)
  monkeypatch.setattr(ncm_server, 'transport_class', lambda: lambda conn: pytest.fail('unverified peer admitted'))
  opener = ncm_server.tcp_opener('enxVerified')(SimpleNamespace(host=ncm.JETSON_ADDR, port=ncm.PORT))
  assert opener() is None
  assert closed == ['peer', 'listener']
  opener.close()


def test_same_opener_recovers_renamed_host_interface_without_engine_restart(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
  names, indices, listeners, bound = ['enxOld'], {'enxOld': 7}, [], []
  monkeypatch.setattr(ncm, 'host_interface', lambda preferred: names[0])
  def index(name):
    if name not in indices:
      raise OSError('USB removed')
    return indices[name]
  class Listener:
    def __init__(self):
      self.closed, self.arrival = False, None
      listeners.append(self)
    def setsockopt(self, *a):
      pass
    def bind(self, *a):
      pass
    def listen(self, *a):
      pass
    def settimeout(self, *a):
      pass
    def accept(self):
      if self.arrival is not None:
        return self.arrival, (ncm.COMMA_ADDR, 1)
      raise TimeoutError
    def close(self):
      self.closed = True
  monkeypatch.setattr(ncm.socket, 'if_nametoindex', index)
  monkeypatch.setattr(ncm.socket, 'socket', lambda *a: Listener())
  monkeypatch.setattr(ncm, 'require_usb_route', lambda *a, **kw: None)
  monkeypatch.setattr(ncm, 'bind_device', lambda sock, iface: bound.append(iface))
  monkeypatch.setattr(ncm_server, 'transport_class', lambda: lambda conn: SimpleNamespace(conn=conn))
  opener = ncm_server.tcp_opener('enxOld')(SimpleNamespace(host=ncm.JETSON_ADDR, port=ncm.PORT))
  assert opener() is None
  indices.clear()
  assert opener() is None and listeners[0].closed
  names[0], indices['enxNew'] = 'enxNew', 8
  assert opener() is None and len(listeners) == 2
  connection = SimpleNamespace(close=lambda: None)
  listeners[-1].arrival = connection
  assert opener().peer == f'ncm:enxNew:{ncm.COMMA_ADDR}'
  assert bound == ['enxOld', 'enxNew', 'enxNew']
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
