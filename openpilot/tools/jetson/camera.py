"""Small HUD camera previews; no camera device owner or DM inference.

The existing NEXO side/reverse view uses camerad's DRIVER stream as a display
input. Forwarding that existing image does not start a DM process.
Preview sampling follows Carrot Jetlink hud_camera.py; renderer stays NEXO.
"""
import base64
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json, read_fresh


def jpeg_preview(frame):
  import numpy as np
  from PIL import Image
  width = 384
  height = min(384, max(2, int(width * frame.height / frame.width) // 2 * 2))
  raw = np.frombuffer(frame.data, np.uint8)
  y = raw[:frame.uv_offset].reshape(-1, frame.stride)
  uv = raw[frame.uv_offset:frame.uv_offset + frame.height // 2 * frame.stride].reshape(-1, frame.stride)
  yi = np.arange(height) * frame.height // height
  xi = np.arange(width) * frame.width // width
  yuv = np.empty((height, width, 3), np.uint8)
  yuv[:, :, 0] = y[np.ix_(yi, xi)]
  for plane in (0, 1):
    yuv[:, :, plane + 1] = uv[np.ix_(yi // 2, xi // 2 * 2 + plane)]
  image = Image.fromarray(yuv, 'YCbCr')
  for quality in (65, 45, 25):
    output = BytesIO()
    image.save(output, format='JPEG', quality=quality)
    if output.tell() <= 32 * 1024:
      return width, height, output.getvalue()
  raise ValueError('camera preview exceeds budget')


class CameraPublisher:
  def __init__(self):
    self.path = RUNTIME / f'previews-{os.getpid()}.json'
    self.process = subprocess.Popen([sys.executable, '-m', 'openpilot.tools.jetson.camera', str(self.path), str(os.getpid())],
                                    stdin=subprocess.DEVNULL)

  @property
  def latest(self):
    return (read_fresh(self.path, .3, limit=160 * 1024) or {}).get('cameras', {})

  def close(self):
    self.process.terminate()
    try:
      self.process.wait(timeout=1)
    except subprocess.TimeoutExpired:
      self.process.kill()
      self.process.wait(timeout=1)
    self.path.unlink(missing_ok=True)


def publish(path, parent):
  import ctypes
  import signal
  # A blocked VisionIPC handshake must not orphan this optional preview child.
  ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
  if os.getppid() != parent:
    return
  from msgq.visionipc import VisionIpcClient, VisionStreamType
  from openpilot.common.params import Params
  from openpilot.cereal import messaging
  os.nice(5)
  params = Params()
  sm = messaging.SubMaster(['carState'])
  streams = {'road': VisionStreamType.VISION_STREAM_ROAD, 'wide': VisionStreamType.VISION_STREAM_WIDE_ROAD,
             'driver': VisionStreamType.VISION_STREAM_DRIVER}
  clients = {}
  while os.getppid() == parent:
    started = time.monotonic()
    sm.update(0)
    side = params.get_int('ClusterHudSideCamera') > 0
    reverse = sm.alive['carState'] and str(sm['carState'].gearShifter) == 'reverse'
    result = {}
    for name, stream in streams.items():
      if name == 'driver' and not (side or reverse):
        clients.pop(name, None)
        continue
      try:
        if name not in clients:
          clients[name] = VisionIpcClient('camerad', stream, conflate=True)
        client = clients[name]
        if not client.is_connected() and not client.connect(False):
          continue
        frame = client.recv(timeout_ms=0)
        if frame is not None:
          width, height, jpeg = jpeg_preview(frame)
          result[name] = {'width': width, 'height': height, 'frame': client.frame_id,
                          'time': time.monotonic(), 'jpeg': base64.b64encode(jpeg).decode()}
      except Exception:
        clients.pop(name, None)
    atomic_json(path, {'updated': time.monotonic(), 'cameras': result})
    time.sleep(max(0, started + .1 - time.monotonic()))


class RemoteCamera:
  def __init__(self, driver=False):
    self.driver = driver
    self.is_wide = False
    self.texture = None
    self.last_frame = None

  def select_stream(self, prefer_wide):
    self.is_wide = bool(prefer_wide)
    return self.is_wide

  def draw(self, destination, *, fit=False, mirror=False, crop=None):
    from openpilot.tools.jetson.snapshot import read_snapshot
    from cluster_side_camera import source_crop_rect
    from cluster_reverse import contained_rect
    import pyray as rl
    from PIL import Image
    value = read_snapshot()
    if value is None:
      return False
    name = 'driver' if self.driver else 'wide' if self.is_wide else 'road'
    frame = value.get('cameras', {}).get(name)
    if frame is None or not 0 <= value['sent'] - frame['time'] < .3:
      return False
    width, height = frame['width'], frame['height']
    if width != 384 or not 2 <= height <= 384:
      return False
    key = (value['session'], name, frame['frame'])
    if key != self.last_frame:
      raw = base64.b64decode(frame['jpeg'], validate=True)
      if len(raw) > 32 * 1024:
        return False
      with Image.open(BytesIO(raw)) as image:
        if image.size != (width, height):
          return False
        rgb = image.convert('RGB').tobytes()
      pointer = rl.ffi.cast('void *', rl.ffi.from_buffer(rgb))
      if self.texture is not None and (self.texture.width, self.texture.height) != (width, height):
        self.close()
      if self.texture is None:
        self.texture = rl.load_texture_from_image(rl.Image(pointer, width, height, 1, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8))
        rl.set_texture_filter(self.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
      else:
        rl.update_texture(self.texture, pointer)
      self.last_frame = key
    fit, mirror = (crop is None, True) if self.driver else (fit, mirror)
    source = rl.Rectangle(0, 0, width, height)
    if crop is not None:
      source = rl.Rectangle(*source_crop_rect(width, height, destination.width, destination.height, *crop))
    elif fit:
      destination = rl.Rectangle(*contained_rect(width, height, (destination.x, destination.y, destination.width, destination.height)))
    if mirror:
      source.width = -source.width
    rl.draw_texture_pro(self.texture, source, destination, rl.Vector2(0, 0), 0, rl.WHITE)
    return True

  def close(self):
    import pyray as rl
    if self.texture is not None:
      rl.unload_texture(self.texture)
      self.texture = None
    self.last_frame = None


if __name__ == '__main__':
  publish(Path(sys.argv[1]), int(sys.argv[2]))
