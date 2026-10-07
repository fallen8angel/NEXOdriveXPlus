"""Display-only YOLO mailbox. No control/radar/CAN publication."""
from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite, decode_json


def input_epoch():
  try:
    raw = (RUNTIME / 'video-input.json').read_bytes()
    value = decode_json(raw, 4096)
    return value.get('epoch')
  except (OSError, ValueError):
    return None


def publish_objects(payload, now):
  if payload.get('magic') != 'NEXO_JETSON_YOLO':
    raise ValueError('wrong detection source')
  width, height = payload.get('width'), payload.get('height')
  if not finite(width) or not finite(height) or not (0 < width <= 8192 and 0 < height <= 8192):
    raise ValueError('invalid detection image size')
  objects = []
  for obj in payload.get('objects', [])[:64]:
    if not isinstance(obj, dict) or not all(finite(obj.get(k)) for k in ('conf', 'x1', 'y1', 'x2', 'y2')):
      continue
    if not (0 <= obj['conf'] <= 1 and 0 <= obj['x1'] < obj['x2'] <= width and 0 <= obj['y1'] < obj['y2'] <= height):
      continue
    objects.append({k: obj[k] for k in ('conf', 'x1', 'y1', 'x2', 'y2')} | {'name': str(obj.get('name', ''))[:64]})
  atomic_json(RUNTIME / 'yolo.json', {'updated': now, 'version': 1, 'display_only': True,
              'width': width, 'height': height, 'encode_id': payload.get('encode_id'), 'objects': objects})
