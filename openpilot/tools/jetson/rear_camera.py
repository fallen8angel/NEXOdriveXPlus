"""Optional rear-facing V4L2 camera on Jetson. Never auto-select a video node."""
import logging
from pathlib import Path
import threading
import time

DEVICE = Path('/dev/nexo-hud-rear')
WIDTH, HEIGHT = 384, 216
log = logging.getLogger(__name__)


class RearCamera:
  def __init__(self):
    self.worker = None
    self.stop = threading.Event()
    self.latest = None
    self.texture = None
    self.uploaded = None
    self.retry_at = 0.

  def _capture(self):
    # Device discovery, negotiation and unplug cleanup can block. Only this
    # optional camera worker touches V4L2, never the HUD render/encoder thread.
    pipeline = None
    try:
      import gi
      gi.require_version('Gst', '1.0')
      from gi.repository import Gst
      Gst.init(None)
      pipeline = Gst.parse_launch(
        f'v4l2src device={DEVICE} ! decodebin ! videoconvert ! videoscale add-borders=true ! '
        f'video/x-raw,format=RGB,width={WIDTH},height={HEIGHT},pixel-aspect-ratio=1/1 ! '
        'appsink name=rear sync=false max-buffers=1 drop=true')
      sink = pipeline.get_by_name('rear')
      if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        return
      deadline = time.monotonic() + 2.
      while not self.stop.is_set() and time.monotonic() < deadline:
        sample = sink.emit('try-pull-sample', 100 * Gst.MSECOND)
        if sample is None:
          continue
        buf, clock = sample.get_buffer(), pipeline.get_clock()
        if clock is None or buf.pts == Gst.CLOCK_TIME_NONE or buf.get_size() != WIDTH * HEIGHT * 3:
          continue
        pts = sample.get_segment().to_running_time(Gst.Format.TIME, buf.pts)
        age = (clock.get_time() - pipeline.get_base_time() - pts) / Gst.SECOND
        if not 0 <= age < .3:
          continue
        now = time.monotonic()
        self.latest = (now - age, buf.extract_dup(0, buf.get_size()))
        deadline = now + 2.
    except Exception as exc:
      log.warning('Rear HUD camera unavailable: %s', exc)
    finally:
      if pipeline is not None:
        pipeline.set_state(Gst.State.NULL)
      self.latest = None

  def draw(self, rl, x, y, w, h, now):
    if not (self.worker and self.worker.is_alive()) and now >= self.retry_at:
      self.retry_at = now + 3.
      if DEVICE.is_char_device():
        self.stop.clear()
        self.worker = threading.Thread(target=self._capture, name='hud-rear-camera', daemon=True)
        self.worker.start()
    frame = self.latest
    if self.stop.is_set() or frame is None or not 0 <= now - frame[0] < .3:
      return False
    if frame[0] != self.uploaded:
      ptr = rl.ffi.cast('void *', rl.ffi.from_buffer(frame[1]))
      if self.texture is None:
        self.texture = rl.load_texture_from_image(rl.Image(ptr, WIDTH, HEIGHT, 1, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8))
        rl.set_texture_filter(self.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
      else:
        rl.update_texture(self.texture, ptr)
      self.uploaded = frame[0]
    scale = min(w / WIDTH, h / HEIGHT)
    dw, dh = WIDTH * scale, HEIGHT * scale
    rl.draw_texture_pro(self.texture, rl.Rectangle(0, 0, -WIDTH, HEIGHT),
                        rl.Rectangle(x + (w-dw)/2, y + (h-dh)/2, dw, dh), rl.Vector2(0, 0), 0, rl.WHITE)
    return True

  def close(self):
    self.stop.set()
    self.retry_at = 0.  # A new R engagement can open a reconnected camera immediately.
    self.latest = self.uploaded = None
    if self.texture is not None:
      import pyray as rl
      rl.unload_texture(self.texture)
      self.texture = None
