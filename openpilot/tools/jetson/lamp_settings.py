"""Display-only preferences. Missing or malformed settings disable the feature."""
import json
from pathlib import Path

from openpilot.tools.jetson.state import atomic_json

PATH = Path('/data/carrot/hud-vehicle-lamps.json')
KEYS = ('enabled', 'brake', 'turn', 'hazard')


def valid(value):
  return isinstance(value, dict) and set(value) == set(KEYS) and all(type(v) is bool for v in value.values())


def read():
  try:
    with PATH.open('rb') as src:
      data = json.loads(src.read(512))
    if valid(data):
      return data
  except (ValueError, OSError):
    pass
  return dict.fromkeys(KEYS, False)


def save(value):
  if not valid(value):
    raise ValueError('네 항목 모두 true 또는 false여야 합니다.')
  PATH.parent.mkdir(parents=True, exist_ok=True)
  atomic_json(PATH, value)
