STOPPING_SPEED_MIN = 10  # hundredths of m/s
STOPPING_SPEED_DEFAULT = 50


def _clamp_stopping_speed(value) -> int:
  try:
    return max(STOPPING_SPEED_MIN, int(value))
  except (TypeError, ValueError, OverflowError):
    return STOPPING_SPEED_DEFAULT


def get_stopping_speed(params) -> float:
  # Use the typed INT reader: malformed persisted values become None, not a
  # native get_float conversion failure. Keep runtime reads free of writes.
  return _clamp_stopping_speed(params.get("VEgoStopping")) * 0.01


def normalize_stopping_speed(params) -> None:
  """Repair legacy settings once at startup, before driving processes start."""
  stored = params.get("VEgoStopping")
  value = _clamp_stopping_speed(stored)
  if stored != value:
    params.put_int("VEgoStopping", value)
