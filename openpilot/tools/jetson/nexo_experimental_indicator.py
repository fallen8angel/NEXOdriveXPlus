"""Jetson-only indicator based on live, read-only selfdriveState.experimentalMode."""
import base64
from pathlib import Path
import os

from openpilot.tools.jetson.state import RUNTIME

from openpilot.tools.jetson.snapshot import read_snapshot


# The exact user-supplied red experiment-mode icon. Embedded so the Jetson HUD
# runtime needs no separate binary asset or vehicle-side update.
ICON_PNG_BASE64 = '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'
ICON_PATH = RUNTIME / 'nexo-experimental-icon.png'


def ensure_icon():
  """Materialize the bundled read-only image in the HUD's runtime RAM folder."""
  payload = base64.b64decode(ICON_PNG_BASE64, validate=True)
  if ICON_PATH.is_file() and ICON_PATH.read_bytes() == payload:
    return ICON_PATH
  ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
  tmp = ICON_PATH.with_name(f'{ICON_PATH.name}.{os.getpid()}.tmp')
  try:
    tmp.write_bytes(payload)
    os.replace(tmp, ICON_PATH)
  finally:
    tmp.unlink(missing_ok=True)
  return ICON_PATH



def experimental_enabled():
  value = read_snapshot()  # Drops to False if USB data are stale or absent.
  if not value:
    return False
  for service in ('selfdriveState', 'controlsState'):
    if not value.get('alive', {}).get(service) or not value.get('valid', {}).get(service):
      continue
    encoded = value.get('events', {}).get(service)
    if not encoded:
      continue
    try:
      from openpilot.cereal import log
      raw = base64.b64decode(encoded, validate=True)
      with log.Event.from_bytes(raw) as event:
        if event.which() != service:
          continue
        return bool(getattr(getattr(event, service), 'experimentalMode', False))
    except (Exception,):
      continue
  return False


def install(renderer):
  cls = renderer.ClusterUiRenderer
  if getattr(cls, '_nexo_exp_installed', False):
    return
  previous_draw = cls._draw_drive_status
  previous_close = cls.close

  def draw(self, state):
    previous_draw(self, state)
    if not experimental_enabled():
      return
    if not hasattr(self, '_nexo_exp_icon_texture'):
      try:
        self._nexo_exp_icon_texture = self._load_icon_texture(ensure_icon(), 'NEXO experimental')
      except (OSError, ValueError) as exc:
        print(f'NEXO experimental icon unavailable: {exc}', flush=True)
        self._nexo_exp_icon_texture = None
    texture = self._nexo_exp_icon_texture
    if texture is None:
      return
    self._draw_outlined_bottom_aligned_texture_icon(
      texture,
      renderer.WIFI_STATUS_CENTER_X + 65,
      renderer.TOP_STATUS_CENTER_Y + renderer.WIFI_STATUS_ICON_SIZE * 0.5,
      43.0, 43.0, (255, 255, 255), 255,
    )

  def close(self, *args, **kwargs):
    icon = getattr(self, '_nexo_exp_icon_texture', None)
    if icon is not None:
      self._nexo_exp_icon_texture = None
      try:
        renderer.rl.unload_texture(icon)
      except Exception:
        pass
    return previous_close(self, *args, **kwargs)

  cls._draw_drive_status = draw
  cls.close = close
  cls._nexo_exp_installed = True
