UI_BORDER_SIZE = 30

# Load NEXO onroad HUD compatibility hooks before any onroad renderer modules.
# This guarantees the persistent vehicle-blinker lane-change arrows are patched
# into HudRenderer regardless of cruise/MED/openpilot engagement state.
try:
  import openpilot.selfdrive.ui.onroad  # noqa: F401
except Exception:
  pass
