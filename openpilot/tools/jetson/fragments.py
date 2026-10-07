"""Bounded display-message fragments below one FunctionFS write quantum.

The reference documents a dwc3 replay hazard for messages split across writev
calls. Fragment large HUD/video/media records at our protocol layer so each
USB frame fits one write, and discard duplicates by the outer sequence number.
"""
import struct

HEADER = struct.Struct('<III')  # transfer id, byte offset, total record length
CHUNK = 128 * 1024 - 32 - HEADER.size
MAX_RECORD = 1024 * 1024


def fragments(raw, transfer):
  if not 0 < len(raw) <= MAX_RECORD:
    raise ValueError('display record exceeds allocation')
  for offset in range(0, len(raw), CHUNK):
    yield HEADER.pack(transfer, offset, len(raw)) + raw[offset:offset + CHUNK]


class Assembler:
  def __init__(self):
    self.pending = {}

  def feed(self, kind, raw, now):
    if not HEADER.size < len(raw) <= HEADER.size + CHUNK:
      raise ValueError('invalid display fragment')
    transfer, offset, total = HEADER.unpack_from(raw)
    piece = raw[HEADER.size:]
    if not 0 < total <= MAX_RECORD or offset + len(piece) > total:
      raise ValueError('invalid display record length')
    if offset == 0:
      self.pending[kind] = (transfer, total, now, bytearray())
    item = self.pending.get(kind)
    if item is None:
      return None
    current, size, started, data = item
    if (current, size, len(data)) != (transfer, total, offset) or not 0 <= now - started < 1:
      self.pending.pop(kind, None)
      return None
    data.extend(piece)
    if len(data) != total:
      return None
    del self.pending[kind]
    return bytes(data)
