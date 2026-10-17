"""Jetlink wire binding over NEXO's existing hardened FunctionFS transport."""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

from openpilot.tools.jetson.transport.ffs import FfsTransport
from openpilot.tools.jetson.transport.base import StreamTransport, LinkError
from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.selfdrive.modeld.jetlink.compat import wire_protocol


class UnsafeCleanupError(RuntimeError):
  pass


class JetlinkFfsTransport(FfsTransport):
  # The low-level NEXO FunctionFS code was already adapted from Jetlink.  Keep
  # its watchdog/reconnect implementation, but select strict JLNK v2/v3 framing
  # for this process only.  The display process continues using NEXD framing.
  wire_protocol = P
  tx_align = P.GADGET_TX_ALIGN

  def __init__(self, mount: str, gadget: str, udc: str | None = None, version=2):
    self.wire_protocol = wire_protocol(version)
    super().__init__(mount, gadget=gadget, udc=udc)

  def close(self):
    reader = getattr(self, '_reader', None)
    super().close()
    for thread in (reader, getattr(getattr(self, '_write_guard', None), 'thread', None)):
      if thread is not None and thread.is_alive():
        raise UnsafeCleanupError('USB cleanup left an active worker; refusing to rebind')
    if self.gadget:
      try:
        if (Path(self.gadget) / 'UDC').read_text().strip():
          raise UnsafeCleanupError('USB cleanup did not release the controller')
      except OSError as exc:
        raise UnsafeCleanupError(f'Cannot verify USB cleanup: {exc}') from exc
    # Both workers are joined and our UDC is released. The stopped watchdog's
    # bound abort callback otherwise keeps this session and its buffers alive
    # until cyclic GC, overlapping repeated retries. Break only our closed
    # session's cycle; never collect/trim the active inference or HUD process.
    self._write_guard._abort = None
    self._chunks.clear()
    self._free.clear()

  def _configured(self):
    self._write_timeout()
    return super()._configured()

  def _wait_for_host_ready(self):
    self._write_timeout()
    return super()._wait_for_host_ready()


class JetlinkTcpTransport(StreamTransport):
  """Explicit opt-in TCP endpoint; does not create, release or probe a gadget."""
  def __init__(self, sock, version=2):
    super().__init__()
    self.sock = sock
    self.wire_protocol = wire_protocol(version)

  @classmethod
  def connect(cls, host, version=2):
    host = str(ipaddress.ip_address(host))
    sock = socket.create_connection((host, 5599), timeout=3.)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return cls(sock, version)

  def _write(self, bufs):
    self.sock.settimeout(self._write_timeout())
    try:
      # sendmsg is unavailable on Windows; partial writes are handled by base.
      return self.sock.send(bufs[0])
    except OSError as exc:
      raise LinkError(str(exc)) from exc

  def _read_into(self, dest, timeout):
    self.sock.settimeout(timeout)
    try:
      size = self.sock.recv_into(dest)
    except TimeoutError:
      return 0
    except OSError as exc:
      raise LinkError(str(exc)) from exc
    if not size:
      raise LinkError('Jetlink TCP peer disconnected')
    return size

  def close(self):
    self.sock.close()
