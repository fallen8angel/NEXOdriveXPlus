"""Experimental JLNK v2 over the already configured AGNOS USB-NCM g1 network.

This is explicitly opt-in: it does NOT create or unbind any USB gadget, turn
off ADB, bring interfaces up, or change addresses/routes. The comma USB-NCM
interface must already have 192.168.100.127/24, and the Jetson host-side NCM
interface must have 192.168.100.250/24. The host must run the matching NEXO
server with --transport tcp --host 192.168.100.250 --port 5599.

Do not use a general Wi-Fi route as an inference transport. This code is
experimental and must be exercised parked/offroad before any driving.
"""
from __future__ import annotations

import json
import select
import socket
import subprocess
import time
from pathlib import Path

from openpilot.tools.jetson.transport.base import LinkError, StreamTransport
from openpilot.selfdrive.modeld.jetlink import protocol as P

MODE_FILE = Path('/data/nexo_jetlink_transport')
USB_IFACE = 'usb0'
COMMA_ADDR = '192.168.100.127'
JETSON_ADDR = '192.168.100.250'
PORT = 5599
NET_CLASS = Path('/sys/class/net')
GADGET_ROOT = Path('/sys/kernel/config/usb_gadget')


def transport_mode() -> str:
  """Default remains dedicated USB FunctionFS; unknown modes fail closed."""
  try:
    value = MODE_FILE.read_text().strip()
  except FileNotFoundError:
    return 'usb'
  except OSError as exc:
    raise RuntimeError(f'cannot read Jetlink transport selection: {exc}') from exc
  if value not in ('usb', 'ncm-tcp'):
    raise ValueError(f'unsupported NEXO Jetlink transport mode: {value!r}')
  return value


def ip_json(*args):
  try:
    result = subprocess.run(
      ['ip', '-j', '-4', *args],
      capture_output=True, text=True, timeout=3, check=False,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise LinkError(f'cannot inspect USB-NCM network: {exc}') from exc
  if result.returncode:
    raise LinkError(f'cannot inspect USB-NCM network: {result.stderr.strip()}')
  try:
    value = json.loads(result.stdout)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
      raise ValueError('expected ip JSON records')
    return value
  except ValueError as exc:
    raise LinkError('invalid ip JSON response') from exc


def require_usb_route(iface=USB_IFACE, local=COMMA_ADDR, peer=JETSON_ADDR, *, host=False) -> str:
  """Read-only preflight. Addresses are a provisioned pair, not discovered identity."""
  net = NET_CLASS / iface
  if not iface or '/' in iface or not net.is_dir():
    raise LinkError(f'USB-NCM interface {iface!r} is missing')
  if host:
    # USB Ethernet/Wi-Fi adapters are not the comma's CDC-NCM connection.
    if (net / 'device/driver').resolve().name != 'cdc_ncm':
      raise LinkError(f'{iface} is not a cdc_ncm USB host interface')
  else:
    functions = GADGET_ROOT.glob('*/configs/*/ncm.*')
    if not any((f / 'ifname').read_text().strip() == iface and
               (f.parents[2] / 'UDC').read_text().strip() for f in functions):
      raise LinkError(f'{iface} is not an active gadget NCM function')
  addresses = ip_json('addr', 'show', 'dev', iface)
  if not any(row.get('ifname') == iface and 'UP' in row.get('flags', []) and
             any(a.get('local') == local and a.get('prefixlen') == 24 for a in row.get('addr_info', []))
             for row in addresses):
    raise LinkError(f'{iface} must be UP with explicitly provisioned {local}/24')
  routes = ip_json('route', 'get', peer, 'from', local)
  if len(routes) != 1 or routes[0].get('dev') != iface or routes[0].get('gateway') or \
     routes[0].get('prefsrc', routes[0].get('src', local)) != local or routes[0].get('type', 'unicast') != 'unicast':
    raise LinkError(f'NCM peer must have a direct route via {iface}; refusing other networks')
  return f'{peer} dev {iface} src {local}'


def bind_device(sock, iface):
  """Pin ingress AND egress for the socket lifetime, including route changes."""
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode('ascii') + b'\0')
  except (AttributeError, OSError, UnicodeError) as exc:
    raise LinkError(f'cannot bind socket to {iface}; Linux SO_BINDTODEVICE permission required') from exc


class NonblockingSocketIO:
  """One reader + one writer; never race by changing a shared socket timeout.

  Carrot's Linux read-ahead thread receives concurrently with server replies.
  A fixed nonblocking socket and separate select deadlines support that safely.
  """

  error_type = LinkError

  def send(self, msg_type, seq, parts=(), flags=0, timeout=None):
    try:
      return super().send(msg_type, seq, parts, flags, timeout=.5 if timeout is None else timeout)
    except Exception:
      self.close()  # Never reuse a stream after a partially sent message.
      raise

  def _write(self, bufs):
    try:
      while True:
        if not select.select([], [self.sock], [], self._write_timeout())[1]:
          raise self.error_type('NCM message send deadline expired')
        try:
          if hasattr(self.sock, 'sendmsg'):
            return self.sock.sendmsg(bufs)
          return self.sock.send(bufs[0])  # Also exercises real TCP on Windows.
        except (BlockingIOError, InterruptedError):
          continue
    except (OSError, ValueError) as exc:
      raise self.error_type(f'NCM send failed: {exc}') from exc

  def _read_into(self, dest, timeout):
    end = None if timeout is None else time.monotonic() + timeout
    try:
      while True:
        remaining = None if end is None else max(0., end - time.monotonic())
        if not select.select([self.sock], [], [], remaining)[0]:
          return 0
        try:
          count = self.sock.recv_into(dest, dest.nbytes)
          if not count:
            raise self.error_type('NCM peer disconnected')
          return count
        except (BlockingIOError, InterruptedError):
          if end is not None and time.monotonic() >= end:
            return 0
    except (OSError, ValueError) as exc:
      raise self.error_type(f'NCM receive failed: {exc}') from exc


class JetlinkNcmTcpTransport(NonblockingSocketIO, StreamTransport):
  """Stream-compatible Jetlink v2 TCP client, isolated to USB-NCM IPv4."""

  wire_protocol = P
  tx_align = 0
  rx_align = 0

  def __init__(self, sock: socket.socket):
    super().__init__()
    self.sock = sock
    sock.setblocking(False)
    for level, option, value in (
      (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
      (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ):
      try:
        sock.setsockopt(level, option, value)
      except OSError:
        pass

  @classmethod
  def connect(cls, timeout: float = 5.0):
    require_usb_route()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
      # Do not rely on DHCP or a Wi-Fi route, even when the address is known.
      bind_device(sock, USB_IFACE)
      sock.bind((COMMA_ADDR, 0))
      sock.settimeout(timeout)
      sock.connect((JETSON_ADDR, PORT))
      if sock.getsockname()[0] != COMMA_ADDR:
        raise LinkError('Jetlink TCP connected with an unexpected source address')
      return cls(sock)
    except Exception:
      sock.close()
      raise

  def close(self):
    try:
      self.sock.close()
    except OSError:
      pass


if __name__ == '__main__':
  print(require_usb_route())
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    bind_device(probe, USB_IFACE)  # No connect, network mutation or gadget writes.
    probe.bind((COMMA_ADDR, 0))
