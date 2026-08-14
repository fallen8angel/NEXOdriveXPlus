import importlib.abc
import importlib.machinery
import sys

import pyray as rl

SIDE_PANEL_WIDTH = 60
_NEXO_HUD_TARGET = "openpilot.selfdrive.ui.mici.onroad.hud_renderer"


def blend_colors(a: rl.Color, b: rl.Color, f: float) -> rl.Color:
  h0, s0, v0 = (hsv0 := rl.color_to_hsv(a)).x, hsv0.y, hsv0.z
  h1, s1, v1 = (hsv1 := rl.color_to_hsv(b)).x, hsv1.y, hsv1.z
  dh = ((h1 - h0 + 180) % 360) - 180  # shortest hue delta
  return rl.color_from_hsv((h0 + f * dh) % 360,
                           s0 + f * (s1 - s0),
                           v0 + f * (v1 - v0))


def _patch_nexo_blind_spot_hud(module) -> None:
  """Show the existing Mici blind-spot icons while NEXO BSM is active."""
  HudRenderer = module.HudRenderer
  if getattr(HudRenderer, "_nexo_blind_spot_hud_patched", False):
    return

  original_init = HudRenderer.__init__
  original_render = HudRenderer._render

  def _init(self):
    original_init(self)
    # Reuse the stock Mici assets. Keep left/right as their own source files.
    self._nexo_blind_spot_left = module.gui_app.texture('icons_mici/onroad/blind_spot_left.png', 134, 150)
    self._nexo_blind_spot_right = module.gui_app.texture('icons_mici/onroad/blind_spot_right.png', 134, 150)

  def _render(self, rect):
    original_render(self, rect)

    try:
      cp = module.ui_state.CP
      fingerprint = getattr(cp, "carFingerprint", None) if cp is not None else None
      if getattr(fingerprint, "name", str(fingerprint)) != "HYUNDAI_NEXO_1ST_GEN":
        return

      car_state = module.ui_state.sm["carState"]
      left_blind_spot = bool(car_state.leftBlindspot)
      right_blind_spot = bool(car_state.rightBlindspot)
    except Exception:
      return

    # Persistent while the vehicle reports a blind-spot object. No blink timer.
    margin = 18
    y = int(rect.y + rect.height - self._nexo_blind_spot_left.height - margin)

    if left_blind_spot:
      module.rl.draw_texture(
        self._nexo_blind_spot_left,
        int(rect.x + margin),
        y,
        module.rl.WHITE,
      )

    if right_blind_spot:
      module.rl.draw_texture(
        self._nexo_blind_spot_right,
        int(rect.x + rect.width - self._nexo_blind_spot_right.width - margin),
        y,
        module.rl.WHITE,
      )

  HudRenderer.__init__ = _init
  HudRenderer._render = _render
  HudRenderer._nexo_blind_spot_hud_patched = True


class _NexoHudPatchLoader(importlib.abc.Loader):
  def __init__(self, loader):
    self.loader = loader

  def create_module(self, spec):
    create = getattr(self.loader, "create_module", None)
    return create(spec) if create is not None else None

  def exec_module(self, module):
    self.loader.exec_module(module)
    _patch_nexo_blind_spot_hud(module)


class _NexoHudPatchFinder(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path, target=None):
    if fullname != _NEXO_HUD_TARGET:
      return None

    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
    if spec is None or spec.loader is None:
      return spec

    spec.loader = _NexoHudPatchLoader(spec.loader)
    return spec


if _NEXO_HUD_TARGET in sys.modules:
  _patch_nexo_blind_spot_hud(sys.modules[_NEXO_HUD_TARGET])
elif not any(type(f).__name__ == "_NexoHudPatchFinder" and type(f).__module__ == __name__ for f in sys.meta_path):
  sys.meta_path.insert(0, _NexoHudPatchFinder())
