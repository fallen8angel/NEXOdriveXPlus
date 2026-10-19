"""Real stream framing and mocked Linux topology; no device configuration."""
import json
import socket
import subprocess
import threading
import time

import pytest

from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout


@pytest.fixture
def network(tmp_path, monkeypatch):
  net = tmp_path / 'net'
  (net / 'usb0').mkdir(parents=True)
  gadget = tmp_path / 'gadgets/g1'
  function = gadget / 'configs/c.1/ncm.0'
  function.mkdir(parents=True)
  (function / 'ifname').write_text('usb0')
  (gadget / 'UDC').write_text('a600000.dwc3')
  monkeypatch.setattr(ncm, 'NET_CLASS', net)
  monkeypatch.setattr(ncm, 'GADGET_ROOT', gadget.parent)
  addr = [{'ifname': 'usb0', 'flags': ['UP'], 'addr_info': [{'local': ncm.COMMA_ADDR, 'prefixlen': 24}]}]
  route = [{'dst': ncm.JETSON_ADDR, 'dev': 'usb0', 'prefsrc': ncm.COMMA_ADDR}]
  calls = []
  def run(args, **kwargs):
    calls.append(args)
    return subprocess.CompletedProcess(args, 0, json.dumps(addr if 'addr' in args else route), '')
  monkeypatch.setattr(ncm.subprocess, 'run', run)
  return addr, route, calls, gadget


def test_transport_mode_is_off_by_default(tmp_path, monkeypatch):
  mode = tmp_path / 'transport'
  monkeypatch.setattr(ncm, 'MODE_FILE', mode)
  assert ncm.transport_mode() == 'usb'
  mode.write_text('ncm-tcp\n')
  assert ncm.transport_mode() == 'ncm-tcp'
  mode.write_text('tcp://0.0.0.0:5599')
  with pytest.raises(ValueError, match='unsupported'):
    ncm.transport_mode()


def test_route_checks_preserve_g1(network):
  _, _, calls, gadget = network
  assert 'dev usb0' in ncm.require_usb_route()
  assert (gadget / 'UDC').read_text() == 'a600000.dwc3'
  assert all('show' in c or 'get' in c for c in calls)


@pytest.mark.parametrize('change', ['wifi', 'gateway', 'source', 'no-address', 'down', 'unbound', 'no-ncm'])
def test_refuse_other_routes_or_unconfigured_ncm(network, change):
  addr, route, _, gadget = network
  if change == 'wifi':
    route[0]['dev'] = 'wlan0'
  elif change == 'gateway':
    route[0]['gateway'] = '192.168.100.1'
  elif change == 'source':
    route[0]['prefsrc'] = '192.168.1.2'
  elif change == 'no-address':
    addr[0]['addr_info'] = []
  elif change == 'down':
    addr[0]['flags'] = []
  elif change == 'unbound':
    (gadget / 'UDC').write_text('')
  else:
    (gadget / 'configs/c.1/ncm.0/ifname').write_text('usb1')
  with pytest.raises(LinkError):
    ncm.require_usb_route()


@pytest.mark.parametrize('stdout,code', [('not json', 0), ('{}', 0), ('[]', 2)])
def test_invalid_ip_output_fails_closed(network, monkeypatch, stdout, code):
  monkeypatch.setattr(ncm.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, code, stdout, 'failed'))
  with pytest.raises(LinkError):
    ncm.require_usb_route()


@pytest.mark.parametrize('denied', [False, True])
def test_device_binding_is_required_before_dial(network, monkeypatch, denied):
  calls = []
  class Socket:
    closed = False
    def setsockopt(self, *args):
      calls.append(('option', args))
      if denied:
        raise PermissionError('CAP_NET_RAW unavailable')
    def bind(self, address):
      calls.append(('bind', address))
    def settimeout(self, timeout):
      pass
    def setblocking(self, value):
      assert value is False
    def connect(self, address):
      calls.append(('connect', address))
    def getsockname(self):
      return ncm.COMMA_ADDR, 123
    def close(self):
      self.closed = True
  sock = Socket()
  monkeypatch.setattr(ncm.socket, 'SO_BINDTODEVICE', 25, raising=False)
  monkeypatch.setattr(ncm.socket, 'socket', lambda *a: sock)
  if denied:
    with pytest.raises(LinkError, match='SO_BINDTODEVICE'):
      ncm.JetlinkNcmTcpTransport.connect()
    assert sock.closed and len(calls) == 1
  else:
    ncm.JetlinkNcmTcpTransport.connect().close()
    assert calls[:3] == [('option', (socket.SOL_SOCKET, 25, b'usb0\0')),
                         ('bind', (ncm.COMMA_ADDR, 0)), ('connect', (ncm.JETSON_ADDR, ncm.PORT))]


@pytest.mark.parametrize('length', [0, 1, 991, 992, 993, 395320])
def test_real_socket_framing_and_padding(length):
  left, right = socket.socketpair()
  a, b = ncm.JetlinkNcmTcpTransport(left), ncm.JetlinkNcmTcpTransport(right)
  errors = []
  def send():
    try:
      a.send(P.Msg.PING, 23, (b'x' * length,), timeout=2)
    except Exception as exc:
      errors.append(exc)
  worker = threading.Thread(target=send)
  worker.start()
  try:
    msg = b.recv(timeout=2)
    assert msg.msg_type == P.Msg.PING and msg.seq == 23
    assert bytes(msg.payload) == b'x' * length
    b.send(P.Msg.PONG, 24, (b'ok',))
    assert bytes(a.recv(.5).payload) == b'ok'
  finally:
    worker.join(3)
    a.close()
    b.close()
  assert not worker.is_alive() and not errors


def test_fragment_timeout_resumes_and_eof_requires_reconnect():
  left, right = socket.socketpair()
  receiver = ncm.JetlinkNcmTcpTransport(right)
  wire = P.pack_header(P.Msg.PING, 1, 4) + b'test'
  try:
    left.sendall(wire[:7])
    with pytest.raises(LinkTimeout):
      receiver.recv(.02)
    left.sendall(wire[7:])
    assert bytes(receiver.recv(.5).payload) == b'test'
    left.close()
    with pytest.raises(LinkError, match='disconnected'):
      receiver.recv(.5)
  finally:
    left.close()
    receiver.close()


@pytest.mark.parametrize('wire', [b'bad!' + P.pack_header(P.Msg.PING, 1, 0)[4:],
                                 P.pack_header(P.Msg.PING, 1, (1 << 20) + 1)])
def test_corrupt_header_never_reused(wire):
  left, right = socket.socketpair()
  receiver = ncm.JetlinkNcmTcpTransport(right)
  try:
    left.sendall(wire)
    with pytest.raises(LinkError):
      receiver.recv(.5)
    with pytest.raises(LinkError, match='desynced'):
      receiver.recv(.5)
  finally:
    left.close()
    receiver.close()


def test_default_deadline_is_total_even_with_partial_writes(monkeypatch):
  left, right = socket.socketpair()
  sender = ncm.JetlinkNcmTcpTransport(left)
  now = [0.]
  monkeypatch.setattr(time, 'monotonic', lambda: now[0])
  def write(bufs):
    now[0] += .2
    return 1
  monkeypatch.setattr(sender, '_write', write)
  try:
    with pytest.raises(LinkError, match='timed out'):
      sender.send(P.Msg.PING, 1, [b'payload'])
    assert .5 <= now[0] < .9 and left.fileno() == -1
  finally:
    sender.close()
    right.close()


def test_daemon_ncm_branch_never_calls_gadget_setup():
  import ast
  from pathlib import Path
  from types import SimpleNamespace
  source = Path(ncm.__file__).with_name('daemon.py')
  tree = ast.parse(source.read_text())
  func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'connect_transport')
  sentinel = object()
  env = {'JetlinkNcmTcpTransport': SimpleNamespace(connect=lambda: sentinel),
         'setup_gadget': lambda **kw: pytest.fail('g1 must not be touched')}
  exec(compile(ast.Module(body=[func], type_ignores=[]), str(source), 'exec'), env)
  assert env['connect_transport']('ncm-tcp') is sentinel
  with pytest.raises(ValueError):
    env['connect_transport']('invalid')


def test_server_preflight_rejects_usb_ethernet_not_ncm(network):
  with pytest.raises(LinkError, match='cdc_ncm'):
    ncm.require_usb_route('usb0', ncm.COMMA_ADDR, ncm.JETSON_ADDR, host=True)


def test_stalled_peer_cannot_block_default_send_forever():
  left, right = socket.socketpair()
  left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
  sender = ncm.JetlinkNcmTcpTransport(left)
  started = time.monotonic()
  try:
    with pytest.raises(LinkError):
      # Windows can buffer more than SO_SNDBUF suggests. Saturate its bounded
      # receive window rather than assuming the very first MiB must block.
      for seq in range(64):
        sender.send(P.Msg.PING, seq, [b'x' * (1 << 20)])
    assert time.monotonic() - started < 3
    assert left.fileno() == -1
  finally:
    sender.close()
    right.close()
