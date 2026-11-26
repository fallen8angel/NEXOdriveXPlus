"""Bounded HUD decoration in the existing RGBA/encoder/USB session."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import sqlite3
import time

from openpilot.tools.jetson.state import finite
from openpilot.tools.jetson.hud_ignition import START_WINDOW

ASSET = Path(__file__).with_name('nexo_startup.gif')
CLAIMS = Path('/opt/carrot-jetlink/updates/hud-startup.sqlite3')
FADE_S = .3


def decode(path=ASSET):
  from PIL import Image
  if path.stat().st_size > 2 * 1024 * 1024:
    raise ValueError('startup asset exceeds byte budget')
  with Image.open(BytesIO(path.read_bytes())) as source:
    if (source.format != 'GIF' or source.width * source.height > 1024 * 576 or not 1 < source.n_frames <= 60
        or source.width * source.height * source.n_frames * 4 > 16 * 1024 * 1024):
      raise ValueError('startup asset exceeds frame budget')
    frames, ends, elapsed = [], [], 0.
    for i in range(source.n_frames):
      source.seek(i)
      duration = source.info.get('duration', 0) / 1000
      if not .02 <= duration <= 1.0:
        raise ValueError('invalid GIF timing')
      elapsed += duration
      if elapsed > 6:
        raise ValueError('startup asset exceeds time budget')
      frames.append(source.convert('RGBA').copy())
      ends.append(elapsed)
  return frames, ends


def claim(cycle, path=CLAIMS):
  # Commit BEFORE showing pixels. A crash/write failure may skip animation,
  # but can never replay it after HUD/service/USB recovery. Keep across updates.
  path.parent.mkdir(parents=True, exist_ok=True)
  with sqlite3.connect(path, timeout=0) as db:
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS played (cycle TEXT PRIMARY KEY)')
    cursor = db.execute('INSERT OR IGNORE INTO played VALUES (?)', (cycle,))
    return cursor.rowcount == 1


def eligible(snapshot):
  value = (snapshot or {}).get('hud_startup') or {}
  cycle, age = value.get('cycle'), value.get('age')
  return (cycle if isinstance(cycle, str) and len(cycle) == 32
          and all(c in '0123456789abcdef' for c in cycle)
          and value.get('on') is True and value.get('safe') is True
          and finite(age) and 0 <= age < START_WINDOW else None)


class StartupAnimation:
  def __init__(self, asset=ASSET, claim_cycle=claim, clock=time.monotonic):
    self.claim_cycle, self.clock = claim_cycle, clock
    self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hud-gif')
    self.future = self.pool.submit(decode, asset)
    self.cycle = None
    self.started = None
    self.finished = set()
    self.cached = None
    self.cached_key = None

  def cancel(self):
    if self.cycle:
      self.finished.add(self.cycle)
    self.cycle = self.started = None
    self.cached = self.cached_key = None

  def close(self):
    self.cancel()
    self.pool.shutdown(wait=False, cancel_futures=True)

  def compose(self, frame, source, current, *, portrait=False, flip_x=False):
    from PIL import Image, ImageOps
    cycle = eligible(source)
    if cycle is None or cycle != eligible(current):
      self.cancel()
      return frame
    if self.cycle and cycle != self.cycle:
      self.cancel()
    if cycle in self.finished or not self.future.done():
      return frame
    try:
      frames, ends = self.future.result()
      if self.cycle is None:
        self.finished.add(cycle)
        if not self.claim_cycle(cycle):
          return frame
        self.finished.discard(cycle)
        self.cycle, self.started = cycle, self.clock()
      elapsed = self.clock() - self.started
      if elapsed < 0 or elapsed >= ends[-1] + FADE_S:
        self.cancel()
        return frame
      index = next((i for i, end in enumerate(ends) if elapsed < end), len(frames) - 1)
      raw, width, height = frame
      key = (index, width, height, portrait, flip_x)
      if self.cached_key != key:
        size = (height, width) if portrait else (width, height)
        layer = Image.new('RGBA', size, (0, 0, 0, 255))
        picture = ImageOps.contain(frames[index], size, Image.Resampling.LANCZOS)
        layer.alpha_composite(picture, ((size[0] - picture.width)//2, (size[1] - picture.height)//2))
        if flip_x:
          layer = layer.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if portrait:
          layer = layer.transpose(Image.Transpose.ROTATE_270)
        self.cached, self.cached_key = layer, key
      layer = self.cached
      if elapsed >= ends[-1]:
        # The normal live P frame has already rendered underneath every tick.
        layer = Image.blend(layer, Image.frombytes('RGBA', (width, height), bytes(raw)),
                            min(1., (elapsed - ends[-1]) / FADE_S))
      return layer.tobytes(), width, height
    except Exception as error:
      self.finished.add(cycle)
      self.cancel()
      print('HUD startup skipped:', type(error).__name__, flush=True)
      return frame
