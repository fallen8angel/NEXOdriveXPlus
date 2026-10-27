"""Optional Jetson-only 2D observations. No vehicle publishers or control hooks."""
import os
import time
from pathlib import Path

from openpilot.tools.jetson.state import decode_json, finite

SIGNAL_PATH = Path('/dev/shm/carrot_signal_observation.json')


def signals(value, now):
  if not isinstance(value, dict) or value.get('version') != 1 or value.get('stream') != 'road':
    return []
  stamp = value.get('timestamp')
  if not finite(stamp) or not 0 <= now - stamp < .5 or value.get('size') != [1344, 760]:
    return []
  if type(value.get('frame_id')) is not int or value['frame_id'] < 0 or not isinstance(value.get('session'), str) or not value['session']:
    return []
  tracks = value.get('tracks')
  if not isinstance(tracks, list):
    return []
  out = []
  for track in tracks[:20]:
    if not isinstance(track, dict):
      continue
    # Predicted/coasting tracks are not current detections.
    if not finite(track.get('age')) or track['age'] != 0:
      continue
    box = track.get('box')
    if not isinstance(box, list) or len(box) != 4 or not all(finite(v) for v in box):
      continue
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= 1344 and 0 <= y1 < y2 <= 760):
      continue
    raw = (track.get('evidence') or {}).get('raw') if isinstance(track.get('evidence'), dict) else None
    color = track.get('state')
    color = color if color in ('red', 'green') and raw == color else 'unknown'
    out.append({'kind': color, 'x': (x1 + x2) / 2688, 'y': (y1 + y2) / 1520})
  return out


def signal_snapshot(now):
  try:
    with SIGNAL_PATH.open('rb') as source:
      value = decode_json(source.read(32769), 32768)
    return {'age_s': now - value['timestamp'], 'items': signals(value, now)}
  except (OSError, ValueError, TypeError, KeyError, RecursionError):
    return {'items': []}


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
  if not isinstance(snapshot, dict) or not finite(snapshot.get('updated')):
    return []
  record = snapshot.get('signal_display')
  if not isinstance(record, dict) or not finite(record.get('age_s')):
    return []
  elapsed = now - snapshot['updated']
  if not 0 <= elapsed < .5 or not 0 <= record['age_s'] + elapsed < .5:
    return []
  out = []
  items = record.get('items')
  for item in items[:20] if isinstance(items, list) else []:
    if isinstance(item, dict) and item.get('kind') in ('red', 'green', 'unknown') and all(
        finite(item.get(k)) and 0 <= item[k] <= 1 for k in ('x', 'y')):
      out.append(item)
  return out


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

  def draw(self, state, *args, **kwargs):
    result = original(self, state, *args, **kwargs)
    if not state.onroad:
      return result
    try:
      items = observed_objects(read_objects()) if show_objects else []
      if show_signals:
        items += remote_signals(read_snapshot(), time.monotonic())
      if items:
        # A separate image-coordinate inset: no guessed 3D depth/metres.
        # Rendered before the unchanged speed/navigation/parking/alerts layer.
        rl = renderer.rl
        x, y = int(self.width * .4), int(self.height * .3)
        w, h = max(100, int(self.width * .2)), max(80, int(self.height * .18))
        rl.draw_rectangle(x, y, w, h, rl.Color(12, 18, 24, 210))
        rl.draw_text('CAMERA 2D', x + 5, y + 4, 12, rl.WHITE)
        for item in items:
          color = {'red': rl.RED, 'green': rl.GREEN, 'unknown': rl.GRAY}.get(item['kind'], rl.WHITE)
          draw_icon(rl, item['kind'], x + 14 + item['x'] * (w - 28),
                    y + 28 + item['y'] * (h - 42), color)
    except Exception:
      pass  # An optional overlay cannot interrupt the base HUD.
    return result
  cls._render_world = draw
  cls._nexo_observations = True
