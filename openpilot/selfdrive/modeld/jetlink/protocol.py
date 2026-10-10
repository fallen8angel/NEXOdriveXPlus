"""Minimal Jetlink protocol v2 used by the NEXO Jetson experiment.

Wire values match the Carrot/Jetlink v2 implementation pinned at
ajouatom/openpilot@eb5bbf2720286f17fc55d6eab8a2f7aacecf1f3d.
"""
from __future__ import annotations

import struct
from enum import IntEnum

MAGIC = 0x4B4E4C4A  # b'JLNK'
VERSION = 2
PACKET_MULTIPLE = 1024
GADGET_TX_ALIGN = 16 * PACKET_MULTIPLE
HEADER_FMT = '<IHHIIIQ4x'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
_header = struct.Struct(HEADER_FMT)


class Msg(IntEnum):
  HELLO_REQ = 1
  HELLO_RESP = 2
  ENGINE_REQ = 3
  ENGINE_RESP = 4
  UPLOAD_CHUNK = 5
  UPLOAD_DONE = 6
  PROGRESS = 7
  INFER_REQ = 8
  INFER_RESP = 9
  STATE_REQ = 12
  STATE_RESP = 13
  ERROR = 14
  PING = 15
  PONG = 16
  SHUTDOWN_REQ = 17
  SHUTDOWN_RESP = 18
  HUD = 0x4000
  NAVI_MEDIA = 0x4001


class Flag(IntEnum):
  RESET_QUEUES = 1 << 0
  WANT_STATE = 1 << 1
  PADDED = 1 << 7


INFER_REQ_FMT = '<II'
INFER_REQ_SIZE = struct.calcsize(INFER_REQ_FMT)
_infer_req = struct.Struct(INFER_REQ_FMT)
INFER_RESP_FMT = '<IIIII'
INFER_RESP_SIZE = struct.calcsize(INFER_RESP_FMT)
_infer_resp = struct.Struct(INFER_RESP_FMT)


class ProtocolError(RuntimeError):
  pass


def pack_header(msg_type: int, seq: int, length: int, flags: int = 0, reserved: int = 0) -> bytes:
  return _header.pack(MAGIC, VERSION, int(msg_type), seq, flags, length, reserved)


def unpack_header(buf):
  magic, version, msg_type, seq, flags, length, reserved = _header.unpack_from(buf)
  if magic != MAGIC:
    raise ProtocolError(f'bad magic 0x{magic:08x}')
  if version != VERSION:
    raise ProtocolError(f'peer speaks protocol v{version}, expected v{VERSION}')
  return magic, version, msg_type, seq, flags, length, reserved


def pack_infer_req(frame_id: int, flags: int = 0) -> bytes:
  return _infer_req.pack(frame_id, flags)


def unpack_infer_resp(buf, offset: int = 0):
  return _infer_resp.unpack_from(buf, offset)


class Status(IntEnum):
  OK = 0
  NOT_READY = 1
  BAD_SHAPE = 2
  INFER_FAILED = 3
  NOT_FINITE = 4
