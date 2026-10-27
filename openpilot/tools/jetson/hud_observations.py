"""Optional Jetson-only 2D observations. No vehicle publishers or control hooks."""
import os
import time
from pathlib import Path

from openpilot.tools.jetson.state import decode_json, finite
from openpilot.tools.jetson.hud_signals import RemoteSignals, clean_record, items, sanitize

SIGNAL_PATH = Path('/dev/shm/carrot_signal_observation.json')


def signals(value, now):
  record = clean_record(value, now)
  return items(record) if record is not None else []


def signal_snapshot(now, cameras=None):
  try:
    with SIGNAL_PATH.open('rb') as source:
      value = decode_json(source.read(32769), 32768)
    return sanitize({'version': 2, 'record': value}, now, cameras)
  except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
    return {'version': 2, 'record': None}


def observed_objects(value):
  if not isinstance(value, dict):
    return []
  width, height = value.get('width'), value.get('height')
  if not finite(width) or not finite(height) or not (0 < width <= 8192 and 0 < height <= 8192):
    return []
  kinds = {'car': 'car', 'person': 'person', 'motorcycle': 'motorcycle', 'motorbike': 'motorcycle'}
  out = []
  objects = value.get('objects')
  for obj in objects[:32] if isinstance(objects, list) else []:
    if not isinstance(obj, dict) or obj.get('name') not in kinds:
      continue
    if not all(finite(obj.get(k)) for k in ('x1', 'x2', 'y1', 'y2', 'conf')) or obj['conf'] < .5:
      continue
    if not (0 <= obj['x1'] < obj['x2'] <= width and 0 <= obj['y1'] < obj['y2'] <= height and obj['conf'] <= 1):
      continue
    out.append({'kind': kinds[obj['name']], 'x': (obj['x1'] + obj['x2']) / (2 * width),
                'y': (obj['y1'] + obj['y2']) / (2 * height)})
  return out


def remote_signals(snapshot, now):
  # Stateless compatibility helper; the live renderer retains a RemoteSignals instance.
  return RemoteSignals().read(snapshot, now)


def draw_icon(rl, kind, x, y, color):
  x, y = int(x), int(y)
  if kind == 'car':
    rl.draw_rectangle_lines(x - 9, y - 5, 18, 11, color)
    rl.draw_rectangle_lines(x - 5, y - 9, 10, 5, color)
    for dx in (-6, 6):
      rl.draw_circle(x + dx, y + 6, 3, color)
  elif kind == 'person':
    rl.draw_circle(x, y - 8, 3, color)
    for a, b, c, d in ((0, -4, 0, 4), (-5, 0, 5, 0), (0, 4, -4, 10), (0, 4, 4, 10)):
      rl.draw_line(x + a, y + b, x + c, y + d, color)
  elif kind == 'motorcycle':
    for dx in (-7, 7):
      rl.draw_circle_lines(x + dx, y + 5, 4, color)
    rl.draw_line(x - 7, y + 5, x + 3, y - 3, color)
    rl.draw_line(x + 3, y - 3, x + 7, y + 5, color)
    rl.draw_circle(x, y - 8, 3, color)
  else:
    rl.draw_circle(x, y, 5, color)
    if kind == 'unknown':
      rl.draw_text('?', x - 3, y - 5, 10, rl.BLACK)


def install(renderer):
  cls = renderer.ClusterUiRenderer
  show_objects = os.getenv('NEXO_HUD_OBJECTS') == '1'
  show_signals = os.getenv('NEXO_HUD_SIGNALS') == '1'
  if not (show_objects or show_signals) or getattr(cls, '_nexo_observations', False):
    return
  from openpilot.tools.jetson.yolo import read_objects
  from openpilot.tools.jetson.snapshot import read_snapshot
  original = cls._render_world
  icon_mode = os.getenv('NEXO_HUD_SIGNAL_VIEW') == 'icon'

  def draw(self, state, *args, **kwargs):
    result = original(self, state, *args, **kwargs)
    if not state.onroad:
      return result
    try:
      try:
        items = observed_objects(read_objects()) if show_objects else []
      except Exception:
        items = []
      extra_lights = 0
      if show_signals:
        if not hasattr(self, '_nexo_signal_reader'):
          self._nexo_signal_reader = RemoteSignals()
        try:
          lights = self._nexo_signal_reader.read(read_snapshot(), time.monotonic())
        except Exception:
          self._nexo_signal_reader.read(None, time.monotonic())
          lights = []
        if icon_mode:
          # A compact row in the existing inset, without implying camera alignment.
          extra_lights = max(0, len(lights) - 4)
          lights = sorted(lights, key=lambda light: light['x'])[:4]
          lights = [dict(light, x=(i + 1) / (len(lights) + 1), y=0.) for i, light in enumerate(lights)]
        items += lights
      if items:
        # A separate image-coordinate inset: no guessed 3D depth/metres.
        # Rendered before the unchanged speed/navigation/parking/alerts layer.
        rl = renderer.rl
        x, y = int(self.width * .4), int(self.height * .3)
        w, h = max(100, int(self.width * .2)), max(80, int(self.height * .18))
        rl.draw_rectangle(x, y, w, h, rl.Color(12, 18, 24, 210))
        label = 'SIGNALS' if icon_mode and not show_objects else 'CAMERA 2D'
        if extra_lights:
          label += f' +{extra_lights}'
        rl.draw_text(label, x + 5, y + 4, 12, rl.WHITE)
        for item in items:
          color = {'red': rl.RED, 'green': rl.GREEN, 'unknown': rl.GRAY}.get(item['kind'], rl.WHITE)
          draw_icon(rl, item['kind'], x + 14 + item['x'] * (w - 28),
                    y + 28 + item['y'] * (h - 42), color)
    except Exception:
      pass  # An optional overlay cannot interrupt the base HUD.
    return result
  cls._render_world = draw
  cls._nexo_observations = True
