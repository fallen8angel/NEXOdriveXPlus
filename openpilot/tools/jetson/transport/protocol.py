"""Display-only framing adapted from Jetlink (see ../LICENSE.jetlink).

Deliberately incompatible with inference Jetlink. There are no control, upload,
shutdown, model, DM, CAN-transmit or parameter-write message types.
"""
import struct
from enum import IntEnum

MAGIC = 0x4458454E  # NEXD
VERSION = 1
PACKET_MULTIPLE = 1024
GADGET_TX_ALIGN = 16 * PACKET_MULTIPLE
HEADER_FMT = '<IHHIIIQ4x'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
_header = struct.Struct(HEADER_FMT)


class Msg(IntEnum):
  HEARTBEAT = 1
  HUD = 2
  ROAD_VIDEO = 3
  NAVI_MEDIA = 4


class Flag(IntEnum):
  PADDED = 1 << 7


class ProtocolError(RuntimeError):
  pass


def pack_header(msg_type, seq, length, flags=0, reserved=0):
  return _header.pack(MAGIC, VERSION, int(msg_type), seq, flags, length, reserved)


def unpack_header(buf):
  fields = _header.unpack_from(buf)
  magic, version, kind, _, flags, _, reserved = fields
  if magic != MAGIC or version != VERSION or kind not in Msg._value2member_map_:
    raise ProtocolError('incompatible NEXO display peer')
  if flags & ~int(Flag.PADDED) or reserved:
    raise ProtocolError('unsupported header flags')
  return fields
