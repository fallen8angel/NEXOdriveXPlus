"""N16 OFFLINE diagnostic extraction for a future 2019 NEXO HUD integration.

N16 does not promise an explicit traffic-light output. Raw vectors are NOT
screen/projected coordinates or safe detected objects. This file has no HUD,
CAN, vehicle or network imports and never renders a physical display.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from openpilot.tools.jetson.nexo_n16_registry import load_registry

OFFLINE_GROUPS = ('lane_lines', 'road_edges', 'lead', 'lead_prob', 'plan')
MAX_VECTOR = 18580


def summarize_raw_model_output(values: Sequence[float], *, source: str = 'synthetic') -> dict:
  """Reject unverified output dimensions; expose only bounded offline statistics.

  Deliberately does not publish array contents, 2D pixel coordinates, car
  labels or color/signals: raw model tensors need actual NEXO calibration.
  """
  if source not in ('synthetic', 'offline_reference'):
    raise ValueError('Only synthetic or offline reference data is permitted')
  if len(values) != MAX_VECTOR:
    raise ValueError('N16 model output must contain exactly 18580 values')
  manifest, _ = load_registry()
  if manifest['output_shapes']['outputs'] != [1, MAX_VECTOR]:
    raise ValueError('Pinned Carrot N16 output contract changed')
  result = {}
  for name in OFFLINE_GROUPS:
    a, b = manifest['output_slices'][name]
    finite = True
    peak = 0.0
    for index in range(a, b):
      value = float(values[index])
      if not math.isfinite(value):
        finite = False
        break
      peak = max(peak, abs(value))
    result[name] = {'raw_elements': b - a,
                    'all_values_finite': finite,
                    'max_abs_value': round(peak, 5) if finite else None,
                    'projected_to_hud': False}
  return {'schema_version': 1, 'source': source, 'vehicle': 'Hyundai NEXO (FE), 2019',
          'raw_output_sections': result, 'model_output_count': MAX_VECTOR,
          'traffic_lights_detected': None, 'camera_geometry_verified': False,
          'external_hud_render_allowed': False, 'controls_allowed': False,
          'renderable_objects': [],
          'note': 'Numeric preview only. No confidence calibration, physical HUD output or live detection.'}
