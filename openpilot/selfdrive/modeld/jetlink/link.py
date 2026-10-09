"""Bounded local IPC between modeld and the NEXO Jetlink USB owner.

The USB transport never runs on modeld's realtime frame thread.  This follows
Carrot's split jetlinkd/modeld architecture but keeps NEXO status paths separate.
"""
from __future__ import annotations

import json
from concurrent.futures import Future, InvalidStateError
from pathlib import Path
import os
import socket
import struct
import threading
import time

import numpy as np

from openpilot.selfdrive.modeld.jetlink import SOCKET, STATUS, FAULT
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec

SPEC = ModelSpec.from_dict(json.loads(Path(__file__).with_name('cinque_v2.json').read_text()))
REQUEST = struct.Struct('<IIQ')
REPLY = struct.Struct('<I3I')
MAX_PACKET = 1 << 20


def read_into(sock, result, deadline=None):
  offset = 0
  while offset < result.nbytes:
    if deadline is not None:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        if offset:
          raise ConnectionError('partial IPC packet timed out')
        raise TimeoutError('IPC deadline exceeded')
      sock.settimeout(remaining)
    try:
      n = sock.recv_into(result[offset:])
    except TimeoutError:
      if offset:
        raise ConnectionError('partial IPC packet timed out') from None
      raise
    if not n:
      raise ConnectionError('IPC disconnected')
    offset += n


def send_parts(sock, *parts, deadline=None):
  views = [memoryview(part).cast('B') for part in parts]
  size = sum(v.nbytes for v in views)
  if not 0 < size <= MAX_PACKET:
    raise ValueError('invalid IPC packet size')
  views.insert(0, memoryview(struct.pack('<I', size)))
  while views:
    if deadline is not None:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise TimeoutError('IPC send deadline exceeded')
      sock.settimeout(remaining)
    sent = sock.sendmsg(views) if hasattr(sock, 'sendmsg') else sock.send(views[0])
    if sent <= 0:
      raise ConnectionError('IPC disconnected during send')
    while views and sent >= views[0].nbytes:
      sent -= views.pop(0).nbytes
    if sent:
      views[0] = views[0][sent:]


class PacketReader:
  def __init__(self, capacity):
    if not 0 < capacity <= MAX_PACKET:
      raise ValueError('invalid IPC receive capacity')
    self.header = bytearray(4)
    self.payload = bytearray(capacity)

  def receive(self, sock, deadline=None):
    read_into(sock, memoryview(self.header), deadline)
    size, = struct.unpack('<I', self.header)
    if not 0 < size <= len(self.payload):
      raise ValueError('invalid IPC packet size')
    out = memoryview(self.payload)[:size]
    read_into(sock, out, deadline)
    return out


def state():
  try:
    value = json.loads(STATUS.read_text())
    if 0 <= time.monotonic() - float(value['updated']) < 3:
      return value
  except (OSError, ValueError, KeyError, TypeError):
    pass
  return {'state': 'unavailable'}


def fault_active():
  try:
    return 0 <= time.monotonic() - float(FAULT.read_text()) < 5
  except (OSError, ValueError):
    return False


class Client:
  def __init__(self, path=SOCKET, timeout=.15):
    self.timeout = timeout
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self.sock.settimeout(timeout)
    try:
      self.sock.connect(path)
      hello = PacketReader(64 << 10).receive(self.sock, time.monotonic() + timeout)
      spec = ModelSpec.from_dict(json.loads(bytes(hello))['spec'])
      if spec.to_dict() != SPEC.to_dict():
        raise ValueError('Jetlink local model contract mismatch')
    except Exception:
      self.sock.close()
      raise
    self.timings = (0, 0, 0)
    self.reader = PacketReader(REPLY.size + SPEC.output_nbytes)

  def infer(self, image, packed, frame, reset=False, source_sof=0):
    if image.dtype != np.uint8 or image.shape != SPEC.warped_shape or not image.flags.c_contiguous:
      raise ValueError('invalid warped image')
    if packed.dtype != np.float32 or packed.size != SPEC.packed_nelem or not packed.flags.c_contiguous:
      raise ValueError('invalid recurrent input')
    if not np.all(np.isfinite(packed)):
      raise ValueError('non-finite recurrent input')
    deadline = time.monotonic() + self.timeout
    send_parts(self.sock, REQUEST.pack(frame, int(reset), source_sof), image, packed, deadline=deadline)
    reply = self.reader.receive(self.sock, deadline)
    if len(reply) != REPLY.size + SPEC.output_nbytes:
      raise ValueError('invalid inference reply size')
    fid, *self.timings = REPLY.unpack_from(reply)
    if fid != frame:
      raise ValueError('stale inference reply')
    output = np.frombuffer(reply, np.float32, SPEC.output_nelem, REPLY.size).copy()
    if not np.all(np.isfinite(output)):
      raise ValueError('non-finite inference reply')
    return output

  def close(self):
    self.sock.close()


class ClientConnection:
  def __init__(self):
    self.future = Future()
    threading.Thread(target=self._connect, args=(self.future,), name='nexo-jetlink-connect', daemon=True).start()

  @staticmethod
  def _connect(future):
    client = None
    try:
      if hasattr(os, 'sched_setscheduler'):
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
      client = Client()
      future.set_result(client)
    except InvalidStateError:
      if client is not None:
        client.close()
    except Exception as exc:
      try:
        future.set_exception(exc)
      except InvalidStateError:
        pass

  def close(self):
    if not self.future.cancel():
      try:
        self.future.result().close()
      except Exception:
        pass
