"""Conservative optical green verification for the optional external HUD only.

The existing /dev/shm/carrot_signal_observation.json consumed by optional
control is never changed here. A separate display record contains current-frame
pixels only; no cached greens, inferred lane ownership or movement permission.
"""
from pathlib import Path

import cv2
import numpy as np


HUD_OBSERVATION_PATH = Path('/dev/shm/carrot_hud_signal_observation.json')


def green_lamp_verified(rgb, box):
  """Reject diffuse cyan/sky and green-looking unlit housing without a lamp core.

  Adapted from Carrot's illuminated_green observer; this is a conservative
  visual check, not a traffic-signal or ego-lane classifier.
  """
  if not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) not in (int, float) for v in box):
    return False
  if not np.isfinite(box).all():
    return False
  if not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3:
    return False
  height, width = rgb.shape[:2]
  x1, y1, x2, y2 = map(int, box)
  if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
    return False
  patch = rgb[y1:y2, x1:x2]
  h, w = patch.shape[:2]
  if h < 3 or w < 5:
    return False

  red, green, blue = np.moveaxis(patch.astype(np.int16), -1, 0)
  yy, xx = np.mgrid[:h, :w]
  interior = (xx >= .70 * w) & (xx < .97 * w) & (yy >= .18 * h) & (yy < .82 * h)
  # Sky can be both bright and blue/cyan. An actual green LED needs a
  # green-dominant compact component and contrast with the unlit housing.
  chroma = (green >= 70) & (green - red >= 30) & (green >= .9 * blue) & (
    (green >= .98 * blue) | (green - red >= 60))
  count, labels, stats, _ = cv2.connectedComponentsWithStats((interior & chroma).astype(np.uint8), 8)
  housing = (xx >= .35 * w) & (xx < .70 * w) & (yy >= .18 * h) & (yy < .82 * h)
  if not housing.any():
    return False
  housing_green = float(np.median(green[housing]))
  housing_chroma = float(np.median((green - red)[housing]))
  for k in range(1, count):
    _, _, component_width, component_height, area = map(int, stats[k])
    if area < 2 or not .4 <= component_width / max(1, component_height) <= 2.5:
      continue
    pixel = labels == k
    if (float(np.percentile(green[pixel], 80)) - housing_green >= 18 and
        float(np.percentile((green - red)[pixel], 80)) - housing_chroma >= 12):
      return True
  return False


def filter_for_hud(rgb, result):
  """Return a separate HUD-only result, never mutating the optional-control data."""
  filtered = []
  for item in result['tracks'][:20]:
    evidence = item['evidence']
    if evidence.get('raw') == 'green' and not green_lamp_verified(rgb, item['box']):
      filtered.append({**item, 'state': 'unknown', 'evidence': {**evidence, 'raw': 'unknown'}})
    else:
      filtered.append(item)
  return {**result, 'tracks': filtered}
