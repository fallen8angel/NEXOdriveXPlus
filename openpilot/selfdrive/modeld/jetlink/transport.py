"""Jetlink wire binding over NEXO's existing hardened FunctionFS transport."""
from __future__ import annotations

from openpilot.tools.jetson.transport.ffs import FfsTransport
from openpilot.selfdrive.modeld.jetlink import protocol as P


class JetlinkFfsTransport(FfsTransport):
  # The low-level NEXO FunctionFS code was already adapted from Jetlink.  Keep
  # its watchdog/reconnect implementation, but select the real JLNK v2 framing
  # for this process only.  The display process continues using NEXD framing.
  wire_protocol = P
  tx_align = P.GADGET_TX_ALIGN

  def __init__(self, mount: str, gadget: str, udc: str | None = None):
    super().__init__(mount, gadget=gadget, udc=udc)
    self.wire_protocol = P
