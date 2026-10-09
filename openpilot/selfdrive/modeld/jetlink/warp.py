"""Camera warp adapter for the pinned Jetlink Cinque v2 contract."""
from __future__ import annotations

import numpy as np


class Warp:
  def __init__(self, width: int, height: int, frame_skip: int):
    from tinygrad import Tensor, TinyJit, Device
    from openpilot.selfdrive.modeld.compile_modeld import NV12Frame, make_warp
    from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

    if Device.DEFAULT != 'QCOM':
      raise RuntimeError('NEXO Jetlink camera adapter requires the native QCOM device')
    self.size = get_nv12_info(width, height)[3]
    self.transforms = {k: np.eye(3, dtype=np.float32) for k in ('tfm', 'big_tfm')}
    self.inputs = {k: Tensor(v, device='NPY').realize() for k, v in self.transforms.items()}
    self.run_warp = TinyJit(make_warp(NV12Frame(width, height, *get_nv12_info(width, height)), 512, 256, frame_skip))
    dummy = {k: Tensor(np.zeros(self.size, np.uint8), device='QCOM').realize() for k in ('frame', 'big_frame')}
    for _ in range(3):
      result = self.run_warp(**self.inputs, **dummy).numpy()
    if result.shape != (2, 6, 128, 256) or result.dtype != np.uint8:
      raise ValueError(f'unexpected Jetlink warp output {result.shape} {result.dtype}')
    self.blobs = {}

  def __call__(self, bufs, transforms):
    from tinygrad import Tensor
    frames = {}
    for key, arg in (('img', 'frame'), ('big_img', 'big_frame')):
      source = np.frombuffer(bufs[key].data, np.uint8)
      if source.size != self.size:
        raise ValueError('camera layout changed')
      cache_key = (key, source.ctypes.data)
      if cache_key not in self.blobs:
        if len(self.blobs) >= 64:
          self.blobs.clear()
        self.blobs[cache_key] = Tensor.from_blob(source.ctypes.data, (self.size,), dtype='uint8', device='QCOM')
      frames[arg] = self.blobs[cache_key]
    self.transforms['tfm'][:] = transforms['img']
    self.transforms['big_tfm'][:] = transforms['big_img']
    return self.run_warp(**self.inputs, **frames).numpy()
