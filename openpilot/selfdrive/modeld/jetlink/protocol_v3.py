"""Jetlink v3 framing; the native NEXO model contract remains independent.

Wire reference: carrot-wip f104b56c, third_party/jetlink/protocol_v3.py.
"""
from enum import IntEnum

from openpilot.selfdrive.modeld.jetlink import protocol as V2

MAGIC = V2.MAGIC
VERSION = 3
PACKET_MULTIPLE = 512
GADGET_TX_ALIGN = V2.GADGET_TX_ALIGN
HEADER_SIZE = V2.HEADER_SIZE
INFER_REQ_SIZE = V2.INFER_REQ_SIZE
INFER_RESP_SIZE = V2.INFER_RESP_SIZE
Msg = V2.Msg
Status = V2.Status
ProtocolError = V2.ProtocolError
pack_infer_req = V2.pack_infer_req
unpack_infer_resp = V2.unpack_infer_resp


def unpack_infer_req(buf, offset=0):
  return V2._infer_req.unpack_from(buf, offset)


def pack_infer_resp(frame, status, gpu_us, queue_us, total_us):
  return V2._infer_resp.pack(frame, status, gpu_us, queue_us, total_us)


class Flag(IntEnum):
  RESET_QUEUES = 1 << 0
  WANT_STATE = 1 << 1
  WANT_HIDDEN = 1 << 2
  PADDED = 1 << 7


def pack_header(msg_type, seq, length, flags=0, reserved=0):
  return V2._header.pack(MAGIC, VERSION, int(msg_type), seq, flags, length, reserved)


def unpack_header(buf):
  fields = V2._header.unpack_from(buf)
  if fields[0] != MAGIC or fields[1] != VERSION:
    raise ProtocolError(f'invalid Jetlink v3 header: magic={fields[0]:08x} version={fields[1]}')
  return fields
