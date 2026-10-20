"""Opt-in adapter for the pinned Carrot server on an existing CDC-NCM link.

No gadget/network provisioning. Interface and peer filtering are containment,
not cryptographic peer authentication; use only the dedicated, trusted cable.
"""
import json
import logging
import socket
import time

from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
from openpilot.selfdrive.modeld.jetlink.protocol import Msg

log = logging.getLogger(__name__)


def transport_class():
  # Import only after native_host has loaded the pinned server's package.
  from jetlink.transport.tcp import TcpTransport
  from jetlink.transport.base import LinkError

  class NcmServerTransport(ncm.NonblockingSocketIO, TcpTransport):
    error_type = LinkError

    def __init__(self, sock):
      super().__init__(sock)
      sock.setblocking(False)
      self.last_message = time.monotonic()

    def _fill(self, need, timeout):
      if need > (1 << 20) + 32:
        raise LinkError('NEXO NCM message exceeds 1 MiB limit')
      return super()._fill(need, timeout)

    def recv(self, timeout=None):
      # Bounds idle/partial-message peers that would occupy the single server
      # forever. The read-ahead worker calls this every 100 ms.
      if time.monotonic() - self.last_message >= 60:
        raise LinkError('NCM peer idle or incomplete message for 60 seconds')
      msg = super().recv(timeout=.1 if timeout is None else min(.1, timeout))
      self.last_message = time.monotonic()
      return msg

    def drain(self, timeout):
      # TCP is a fresh stream on reconnect. Upstream's bulk-oriented drain
      # resets its idle timer on incoming bytes and could pin this listener
      # forever after a malformed header from a continuously sending peer.
      self.close()

  return NcmServerTransport


def tcp_opener(iface):
  def create(args):
    if args.host != ncm.JETSON_ADDR or args.port != ncm.PORT:
      raise ValueError('NCM server must use the provisioned USB address and port')
    transport = transport_class()
    listener = None
    interface_index = None
    last_error = ''

    def close():
      nonlocal listener, interface_index
      if listener is not None:
        listener.close()
      listener = interface_index = None

    def listen():
      nonlocal listener, interface_index
      ncm.require_usb_route(iface, ncm.JETSON_ADDR, ncm.COMMA_ADDR, host=True)
      index = socket.if_nametoindex(iface)
      candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      try:
        ncm.bind_device(candidate, iface)
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        candidate.bind((args.host, args.port))
        candidate.listen(1)
        candidate.settimeout(1)
        if socket.if_nametoindex(iface) != index:
          raise ncm.LinkError('NCM interface changed while binding listener')
      except Exception:
        candidate.close()
        raise
      listener, interface_index = candidate, index

    def accept():
      nonlocal last_error
      conn = None
      try:
        # SO_BINDTODEVICE retains the old ifindex across USB re-enumeration.
        # Reopen only the listener, keeping the shared TensorRT engine loaded.
        if listener is not None and socket.if_nametoindex(iface) != interface_index:
          close()
        if listener is None:
          listen()
        conn, addr = listener.accept()
        if addr[0] != ncm.COMMA_ADDR:
          conn.close()
          return None
        ncm.require_usb_route(iface, ncm.JETSON_ADDR, ncm.COMMA_ADDR, host=True)
        if socket.if_nametoindex(iface) != interface_index:
          raise ncm.LinkError('NCM interface changed before accepting peer')
        ncm.bind_device(conn, iface)
        result = transport(conn)
        result.peer = f'ncm:{iface}:{addr[0]}'
        last_error = ''
        return result
      except TimeoutError:
        return None
      except (OSError, ncm.LinkError) as exc:
        if conn is not None:
          conn.close()
        close()
        # Pinned _serve sleeps two seconds on None. Do not restart the GPU
        # service for a late NIC/address or spam identical network failures.
        detail = str(exc)
        if detail != last_error:
          log.warning('NCM listener waiting: %s', detail)
          last_error = detail
        return None
      except Exception:
        if conn is not None:
          conn.close()
        close()
        raise

    accept.close = close
    accept.waiting_detail = f'waiting for {ncm.COMMA_ADDR} on NCM {iface}'
    return accept
  return create


def session_class(base, contract):
  """Disable upstream upload, power-off and Wi-Fi provisioning over TCP."""
  from jetlink.transport.base import LinkError
  allowed = {Msg.HELLO_REQ, Msg.ENGINE_REQ, Msg.INFER_REQ, Msg.STATE_REQ, Msg.PING, Msg.HUD, Msg.NAVI_MEDIA}

  class NcmSession(base):
    def handle(self, msg):
      if msg.msg_type not in allowed:
        raise LinkError(f'unsupported NCM operation {msg.msg_type}')
      if msg.msg_type == Msg.ENGINE_REQ:
        try:
          value = json.loads(bytes(msg.payload))
          expected = (contract.sha256, contract.nbytes, contract.frame_skip)
          actual = (value['sha256'], value['nbytes'], value['frame_skip'])
          if actual != expected or type(value['nbytes']) is not int or type(value['frame_skip']) is not int:
            raise ValueError('model identity mismatch')
        except (ValueError, TypeError, KeyError) as exc:
          raise LinkError('NCM engine request does not match installed NEXO model') from exc
      return super().handle(msg)
  return NcmSession
