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


def _patch_nexo_blinker_hud(module) -> None:
  HudRenderer = module.HudRenderer
  if getattr(HudRenderer, "_nexo_blinker_hud_patched", False):
    return

  original_render = HudRenderer._render

  def _draw_arrow(left: bool, cx: float, cy: float) -> None:
    # Draw the arrow entirely with thick lines. The previous triangle-based
    # arrow could leave only the shaft visible on Mici, which looked like a
    # small green dot/bar.
    color = module.rl.Color(0, 255, 0, 245)
    thickness = 18.0

    if left:
      tip = module.rl.Vector2(cx - 62.0, cy)
      upper = module.rl.Vector2(cx - 10.0, cy - 42.0)
      lower = module.rl.Vector2(cx - 10.0, cy + 42.0)
      tail = module.rl.Vector2(cx + 64.0, cy)
    else:
      tip = module.rl.Vector2(cx + 62.0, cy)
      upper = module.rl.Vector2(cx + 10.0, cy - 42.0)
      lower = module.rl.Vector2(cx + 10.0, cy + 42.0)
      tail = module.rl.Vector2(cx - 64.0, cy)

    module.rl.draw_line_ex(tip, upper, thickness, color)
    module.rl.draw_line_ex(tip, lower, thickness, color)
    module.rl.draw_line_ex(upper if left else tail, tail if left else upper, 0.0, color) if False else None
    module.rl.draw_line_ex(module.rl.Vector2(cx - 8.0 if left else cx + 8.0, cy), tail, thickness, color)

  def _render(self, rect):
    original_render(self, rect)

    try:
      cp = module.ui_state.CP
      fingerprint = getattr(cp, "carFingerprint", None) if cp is not None else None
      if getattr(fingerprint, "name", str(fingerprint)) != "HYUNDAI_NEXO_1ST_GEN":
        return

      car_state = module.ui_state.sm["carState"]
      left = bool(car_state.leftBlinker)
      right = bool(car_state.rightBlinker)
    except Exception:
      return

    if not left and not right:
      return

    center_x = float(rect.x + rect.width / 2.0)
    center_y = float(rect.y + rect.height * 0.60)
    if left and right:
      _draw_arrow(True, center_x - 85.0, center_y)
      _draw_arrow(False, center_x + 85.0, center_y)
    elif left:
      _draw_arrow(True, center_x, center_y)
    else:
      _draw_arrow(False, center_x, center_y)

  HudRenderer._render = _render
  HudRenderer._nexo_blinker_hud_patched = True


class _NexoHudPatchLoader(importlib.abc.Loader):
  def __init__(self, loader):
    self.loader = loader

  def create_module(self, spec):
    create = getattr(self.loader, "create_module", None)
    return create(spec) if create is not None else None

  def exec_module(self, module):
    self.loader.exec_module(module)
    _patch_nexo_blinker_hud(module)


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
  _patch_nexo_blinker_hud(sys.modules[_NEXO_HUD_TARGET])
elif not any(type(f).__name__ == "_NexoHudPatchFinder" and type(f).__module__ == __name__ for f in sys.meta_path):
  sys.meta_path.insert(0, _NexoHudPatchFinder())
