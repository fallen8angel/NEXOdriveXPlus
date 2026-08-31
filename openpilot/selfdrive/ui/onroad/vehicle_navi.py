import math


def vehicle_navi_camera_active(sm) -> bool:
  """Return whether the stock vehicle navigation is currently reporting a camera."""
  try:
    speed_limit = float(sm["carState"].speedLimit)
  except (AttributeError, KeyError, TypeError, ValueError):
    return False
  return math.isfinite(speed_limit) and speed_limit > 0.0


def onroad_speed_source_label(source: str | None) -> str:
  normalized = str(source or "").strip()
  if normalized.lower() == "hda":
    return "vNAVI"
  return (normalized or "apply")[:8]
