"""Guarded switch between the normal NEXO model and Jetson Jetlink inference.

Carrot's JoiningModel architecture is retained, but the model contract is built
from the exact NEXO ModelState that was already loaded. Jetlink therefore
accelerates the existing NEXO model instead of silently replacing it with a
Carrot/Cinque model.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.modeld.jetlink import FAULT, MODEL_STATUS, SPEC_FILE
from openpilot.selfdrive.modeld.jetlink.link import ClientConnection, state
from openpilot.selfdrive.modeld.jetlink.spec import native_spec
from openpilot.selfdrive.modeld.jetlink.warp import Warp
from openpilot.selfdrive.modeld.parse_model_outputs import Parser


def _may_join(sm) -> bool:
  now = time.monotonic()
  required = ('carState', 'selfdriveState', 'carControl')
  if not all(sm.valid[k] and sm.alive[k] and 0 <= now - sm.recv_time[k] < .25 for k in required):
    return False
  cs = sm['carState']
  return (cs.standstill and abs(cs.vEgo) < .01) or cs.steeringPressed


def _publish_spec(spec):
  tmp = SPEC_FILE.with_suffix('.tmp')
  tmp.write_text(json.dumps(spec.to_dict(), separators=(',', ':')))
  os.replace(tmp, SPEC_FILE)


class JoiningModel:
  def __init__(self, small, width: int, height: int):
    self.small = small
    self.usbgpu = False
    self.vision_input_names = list(small.vision_input_names)
    self.spec = native_spec(small)
    _publish_spec(self.spec)
    self.warp = Warp(width, height, self.spec.frame_skip)
    self.parser = Parser()
    self.sm = messaging.SubMaster(['carState', 'selfdriveState', 'carControl'])
    self.client = None
    self.connection = None
    self.active = False
    self.packed = np.zeros(self.spec.packed_nelem, np.float32)
    self.views = {name: arr.reshape(shape) for (name, shape), arr in zip(
      self.spec.packed_shapes.items(), np.split(self.packed, np.cumsum(self.spec.packed_sizes[:-1])), strict=True)}
    self.prev_desire = np.zeros(self.spec.input_shapes['desire_pulse'][2], np.float32)
    self.frame = 0
    self.reset = True
    self.small_runs = 0
    self.next_join = 0.0
    self.next_status = 0.0
    self.ready = False
    self.error = ''

  def _write_status(self):
    now = time.monotonic()
    if now < self.next_status:
      return
    usb = state()
    self.ready = usb.get('state') == 'ready' and usb.get('sha256') == self.spec.sha256
    record = dict(active=self.active, ready=self.ready, model='NEXO native driving_supercombo',
                  sha256=self.spec.sha256, error=self.error, updated=now)
    tmp = MODEL_STATUS.with_suffix('.tmp')
    try:
      tmp.write_text(json.dumps(record, separators=(',', ':')))
      os.replace(tmp, MODEL_STATUS)
    except OSError:
      pass
    self.next_status = now + 1.0

  def _reset_small(self):
    try:
      self.small.prev_desire[:] = 0
      for array in self.small.npy.values():
        array[:] = 0
      for name, tensor in self.small.input_queues.items():
        if name.endswith('_q'):
          tensor._buffer().copyin(memoryview(bytearray(tensor.numel() * tensor.dtype.itemsize)))
    except Exception:
      cloudlog.exception('NEXO Jetlink local model reset failed')

  def _try_join(self):
    if self.client is not None or self.small_runs < 3 or not self.ready or time.monotonic() < self.next_join:
      return
    if not _may_join(self.sm):
      if self.connection is not None:
        self.connection.close()
        self.connection = None
      return
    try:
      if self.connection is None:
        self.connection = ClientConnection(self.spec)
      if self.connection.future.done():
        self.client = self.connection.future.result()
        self.connection = None
        self.packed[:] = 0
        self.prev_desire[:] = 0
        self.reset = True
        self.error = ''
    except Exception as exc:
      self.connection = None
      self.error = str(exc)
      self.next_join = time.monotonic() + 5.0

  def run(self, bufs, transforms, inputs, prepare_only):
    self.sm.update(0)
    self._write_status()
    self._try_join()

    if self.client is not None and not prepare_only:
      try:
        desire = inputs['desire_pulse'].copy()
        desire[0] = 0
        self.views['desire'][:] = np.where(desire - self.prev_desire > .99, desire, 0)
        self.prev_desire[:] = desire
        self.views['traffic_convention'][:] = inputs['traffic_convention']
        self.views['action_t'][:] = inputs['action_t']
        self.frame = (self.frame + 1) & 0xFFFFFFFF
        images = self.warp(bufs, transforms)
        result = self.client.infer(images, self.packed, self.frame, self.reset)
        self.reset = False
        self.views['prev_feat'][:] = result[self.spec.output_slices['hidden_state']]
        if not self.active:
          cloudlog.warning('NEXO Jetlink active: native model %s', self.spec.sha256)
          self.active = True
          self.next_status = 0.0
        parsed = self.parser.parse_outputs({k: result[np.newaxis, v] for k, v in self.spec.output_slices.items()})
        if os.getenv('SEND_RAW_PRED'):
          parsed['raw_pred'] = result.copy()
        return parsed
      except Exception as exc:
        was_active = self.active
        self.error = str(exc)
        try:
          self.client.close()
        except Exception:
          pass
        self.client = None
        self.active = False
        self.next_join = time.monotonic() + 5.0
        self.next_status = 0.0
        if was_active:
          try:
            FAULT.write_text(str(time.monotonic()))
          except OSError:
            pass
          self._reset_small()
          raise RuntimeError('active Jetlink inference failed; forcing safe modeld restart') from exc
        cloudlog.warning('NEXO Jetlink join failed; retaining local model: %s', exc)

    result = self.small.run(bufs, transforms, inputs, prepare_only)
    if result is not None:
      self.small_runs += 1
    return result
