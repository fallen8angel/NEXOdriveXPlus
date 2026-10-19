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

import socket
import subprocess
from pathlib import Path

from openpilot.tools.jetson.transport.base import LinkError, StreamTransport
from openpilot.selfdrive.modeld.jetlink import protocol as P

MODE_FILE = Path('/data/nexo_jetlink_transport')
USB_IFACE = 'usb0'
COMMA_ADDR = '192.168.100.127'
JETSON_ADDR = '192.168.100.250'
PORT = 5599


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


def require_usb_route() -> str:
  """Refuse to route model data over wlan0, tailscale0 or cellular."""
  try:
    result = subprocess.run(
      ['ip', '-4', 'route', 'get', JETSON_ADDR, 'from', COMMA_ADDR],
      capture_output=True, text=True, timeout=3, check=False,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise LinkError(f'cannot inspect usb0 route: {exc}') from exc
  route = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ''
  fields = route.split()
  if result.returncode or not any(fields[i:i + 2] == ['dev', USB_IFACE] for i in range(len(fields) - 1)):
    raise LinkError(f'NCM route not via usb0 (configure USB-NCM first): {route or result.stderr.strip()}')
  # A source-bound socket plus verified routing keeps this path distinct from
  # tethered Wi-Fi. This must be rechecked at every reconnect.
  if not Path(f'/sys/class/net/{USB_IFACE}').exists():
    raise LinkError('USB-NCM interface usb0 is missing')
  return route


class JetlinkNcmTcpTransport(StreamTransport):
  """Stream-compatible Jetlink v2 TCP client, isolated to USB-NCM IPv4."""

  wire_protocol = P
  tx_align = 0
  rx_align = 0

  def __init__(self, sock: socket.socket):
    super().__init__()
    self.sock = sock
    self._timeout = object()
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
      sock.bind((COMMA_ADDR, 0))
      sock.settimeout(timeout)
      sock.connect((JETSON_ADDR, PORT))
      if sock.getsockname()[0] != COMMA_ADDR:
        raise LinkError('Jetlink TCP connected with an unexpected source address')
      return cls(sock)
    except Exception:
      sock.close()
      raise

  def _set_timeout(self, timeout: float | None):
    if timeout != self._timeout:
      self.sock.settimeout(timeout)
      self._timeout = timeout

  def _write(self, bufs: list[memoryview]) -> int:
    # Bound even HELLO/HUD sends that did not pass a frame deadline, so a
    # stalled NCM host cannot block the inference owner indefinitely.
    self._set_timeout(self._write_timeout(0.5))
    try:
      return self.sock.sendmsg(bufs)
    except OSError as exc:
      raise LinkError(f'Jetlink NCM send failed: {exc}') from exc

  def _read_into(self, dest: memoryview, timeout: float | None) -> int:
    self._set_timeout(timeout)
    try:
      n = self.sock.recv_into(dest, dest.nbytes)
    except socket.timeout:
      return 0
    except OSError as exc:
      raise LinkError(f'Jetlink NCM receive failed: {exc}') from exc
    if n == 0:
      raise LinkError('Jetlink NCM peer disconnected')
    return n

  def close(self):
    try:
      self.sock.close()
    except OSError:
      pass
