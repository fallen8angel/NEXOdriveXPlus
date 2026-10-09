"""Minimal Jetlink v2 client for NEXO Jetson inference.

Derived from zoompilot/Jetlink as carried by Carrot. This intentionally omits
model upload, shutdown, mobile and Mac support. The Jetson must already have
the pinned Cinque v2 engine/model available.
"""
from __future__ import annotations

import json
import secrets
import time

import numpy as np

from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout
from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec

FRAME_TIMEOUT = 0.15


class EngineMissing(LinkError):
  pass


def _as_bytes(buf, expect: int, name: str) -> memoryview:
  mv = memoryview(buf)
  if not mv.contiguous:
    raise LinkError(f'{name} must be contiguous')
  mv = mv.cast('B')
  if mv.nbytes != expect:
    raise LinkError(f'{name} is {mv.nbytes} bytes, expected {expect}')
  return mv


class JetlinkClient:
  def __init__(self, transport, deadline: float = FRAME_TIMEOUT, name: str = 'nexo-jetlink'):
    self.t = transport
    self.deadline = deadline
    self.name = name
    self.nonce = secrets.token_hex(4)
    self.seq = 0
    self.spec: ModelSpec | None = None
    self.dead = False
    self.last_timings = (0, 0, 0)
    self.last_state: dict | None = None
    self._engine_state: dict | None = None
    self._infer_started = 0.0
    self._infer_frame_id = 0

  def _next_seq(self):
    self.seq = (self.seq + 1) & 0xFFFFFFFF
    return self.seq

  def _dispatch(self, msg):
    if msg.msg_type == P.Msg.ENGINE_RESP:
      self._engine_state = json.loads(bytes(msg.payload))
    elif msg.msg_type == P.Msg.ERROR:
      data = json.loads(bytes(msg.payload))
      raise LinkError(f"server error: {data.get('error')}: {data.get('detail')}")
    # PROGRESS is deliberately ignored in the NEXO no-upload port.

  def _expect(self, msg_type: int, seq: int, timeout: float | None):
    end = None if timeout is None else time.monotonic() + timeout
    while True:
      remaining = None if end is None else end - time.monotonic()
      if remaining is not None and remaining <= 0:
        raise LinkTimeout(f'timed out waiting for type={msg_type} seq={seq}')
      msg = self.t.recv(timeout=remaining)
      if msg.msg_type == msg_type and msg.seq == seq:
        return msg
      if msg.msg_type in (P.Msg.ENGINE_RESP, P.Msg.PROGRESS, P.Msg.ERROR):
        self._dispatch(msg)
      # Other stale replies are discarded rather than poisoning the next frame.

  def hello(self, timeout: float = 5.0):
    seq = self._next_seq()
    self.t.send_json(P.Msg.HELLO_REQ, seq, {'client': {'nonce': self.nonce, 'name': self.name}})
    peer = json.loads(bytes(self._expect(P.Msg.HELLO_RESP, seq, timeout).payload))
    if type(peer.get('protocol')) is not int or peer['protocol'] != P.VERSION:
      raise LinkError(f"Jetson protocol mismatch: {peer.get('protocol')}")
    return peer

  def ensure_engine(self, wanted: ModelSpec, timeout: float = 30.0):
    self.spec = None
    self._engine_state = None
    seq = self._next_seq()
    self.t.send_json(P.Msg.ENGINE_REQ, seq, {
      'sha256': wanted.sha256,
      'nbytes': wanted.nbytes,
      'frame_skip': wanted.frame_skip,
    })
    self._engine_state = json.loads(bytes(self._expect(P.Msg.ENGINE_RESP, seq, min(timeout, 10.0)).payload))
    if self._engine_state.get('state') == 'need_upload':
      raise EngineMissing('Jetson does not have the pinned Cinque v2 engine/model')

    end = time.monotonic() + timeout
    while self._engine_state.get('state') not in ('ready', 'failed', 'need_upload'):
      remaining = end - time.monotonic()
      if remaining <= 0:
        raise LinkTimeout(f"Jetson engine not ready after {timeout:.0f}s")
      try:
        self._dispatch(self.t.recv(timeout=min(1.0, remaining)))
      except LinkTimeout:
        continue

    state = self._engine_state.get('state')
    if state == 'need_upload':
      raise EngineMissing('Jetson requested model upload; this NEXO port does not upload while driving')
    if state != 'ready':
      raise LinkError(f"Jetson engine failed: {self._engine_state.get('detail', '')}")
    if 'spec' not in self._engine_state:
      raise LinkError('Jetson reported ready without model spec')
    spec = ModelSpec.from_dict(self._engine_state['spec'])
    if spec.to_dict() != wanted.to_dict():
      raise LinkError('Jetson model contract does not match pinned Cinque v2')
    self.spec = spec
    return spec

  def state(self, timeout: float = 1.0):
    seq = self._next_seq()
    self.t.send_json(P.Msg.STATE_REQ, seq, {})
    return json.loads(bytes(self._expect(P.Msg.STATE_RESP, seq, timeout).payload))

  def infer(self, warped: np.ndarray, packed: np.ndarray, frame_id: int = 0,
            reset: bool = False, want_state: bool = False):
    if self.spec is None:
      raise LinkError('ensure_engine() first')
    if self.dead:
      raise LinkError('link previously failed')
    warped_b = _as_bytes(warped, self.spec.warped_nbytes, 'warped')
    packed_b = _as_bytes(packed, self.spec.packed_nbytes, 'packed')
    seq = self._next_seq()
    flags = (P.Flag.RESET_QUEUES if reset else 0) | (P.Flag.WANT_STATE if want_state else 0)
    self._infer_started = time.monotonic()
    self._infer_frame_id = frame_id
    try:
      self.t.send(P.Msg.INFER_REQ, seq, (P.pack_infer_req(frame_id, flags), warped_b, packed_b), timeout=self.deadline)
      remain = self.deadline - (time.monotonic() - self._infer_started)
      if remain <= 0:
        raise LinkTimeout('frame deadline elapsed during send')
      msg = self._expect(P.Msg.INFER_RESP, seq, remain)
      if msg.payload.nbytes < P.INFER_RESP_SIZE:
        raise LinkError('short inference response')
      fid, status, gpu_us, queue_us, total_us = P.unpack_infer_resp(msg.payload)
      self.last_timings = (gpu_us, queue_us, total_us)
      if status != P.Status.OK or fid != frame_id:
        raise LinkError(f'invalid inference response frame={fid} status={status}')
      end = P.INFER_RESP_SIZE + self.spec.output_nbytes
      if msg.payload.nbytes < end:
        raise LinkError('inference response missing model outputs')
      output = np.frombuffer(msg.payload, np.float32, self.spec.output_nelem, P.INFER_RESP_SIZE).copy()
      if not np.all(np.isfinite(output)):
        raise LinkError('non-finite Jetson model output')
      if msg.payload.nbytes > end:
        try:
          self.last_state = json.loads(bytes(msg.payload[end:]))
        except ValueError:
          pass
      return output
    except Exception:
      self.dead = True
      raise

  def close(self):
    self.t.close()
