"""Onroad UI compatibility hooks for first-generation NEXO."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys


_TARGET = "openpilot.selfdrive.ui.onroad.hud_renderer"


def _patch_hud_renderer(module) -> None:
  HudRenderer = module.HudRenderer
  if getattr(HudRenderer, "_nexo_med_hud_patched", False):
    return

  original_render = HudRenderer._render

  def _draw_fallback_arrow(left: bool, cx: float, cy: float) -> None:
    """Draw a bright green arrow without depending on image assets."""
    rl = module.rl
    color = module.COLORS.CARROT_GREEN
    if left:
      p1 = rl.Vector2(cx - 72, cy)
      p2 = rl.Vector2(cx + 30, cy - 58)
      p3 = rl.Vector2(cx + 30, cy + 58)
      rl.draw_triangle(p1, p2, p3, color)
      rl.draw_rectangle(int(cx + 18), int(cy - 18), 72, 36, color)
    else:
      p1 = rl.Vector2(cx + 72, cy)
      p2 = rl.Vector2(cx - 30, cy + 58)
      p3 = rl.Vector2(cx - 30, cy - 58)
      rl.draw_triangle(p1, p2, p3, color)
      rl.draw_rectangle(int(cx - 90), int(cy - 18), 72, 36, color)

  def _draw_med_badge(self, rect, car_state) -> None:
    """Show AI-style green CRUISE/MED availability before a speed is set."""
    try:
      available = bool(car_state.cruiseState.available)
      enabled = bool(car_state.cruiseState.enabled)
    except Exception:
      return
    if not available or enabled:
      return

    rl = module.rl
    w = 176.0
    h = 62.0
    x = float(rect.x + 48.0)
    y = float(rect.y + 232.0)
    badge = rl.Rectangle(x, y, w, h)
    rl.draw_rectangle_rounded(badge, 0.35, 8, rl.Color(0, 85, 0, 210))
    rl.draw_rectangle_rounded_lines_ex(badge, 0.35, 8, 3.0, module.COLORS.CARROT_GREEN)
    text = "CRUISE  MED"
    size = module.measure_text_cached(self._font_semi_bold, text, 28)
    rl.draw_text_ex(
      self._font_semi_bold,
      text,
      rl.Vector2(x + (w - size.x) / 2.0, y + 17.0),
      28,
      0,
      module.COLORS.CARROT_GREEN,
    )

  def _render(self, rect):
    original_render(self, rect)

    try:
      car_state = module.ui_state.sm["carState"]
      left = bool(car_state.leftBlinker)
      right = bool(car_state.rightBlinker)
    except Exception:
      return

    # MED is cruise-ready/lateral-active even before SET/RES. Make that state
    # visually obvious in the same green family used by the Carrot cruise HUD.
    _draw_med_badge(self, rect, car_state)

    if not left and not right:
      return

    icon_size = 190.0
    center_x = float(rect.x + rect.width / 2.0)
    center_y = float(rect.y + rect.height * 0.60)
    tint = module.COLORS.CARROT_GREEN

    def draw_one(is_left: bool, cx: float) -> None:
      tex = self._ic_lane_change_l if is_left else self._ic_lane_change_r
      try:
        if tex is not None and getattr(tex, "width", 0) > 0:
          self._draw_texture_rect(tex, cx - icon_size / 2.0, center_y - icon_size / 2.0,
                                  icon_size, icon_size, tint=tint)
          return
      except Exception:
        pass
      _draw_fallback_arrow(is_left, cx, center_y)

    if left and right:
      draw_one(True, center_x - 125.0)
      draw_one(False, center_x + 125.0)
    elif left:
      draw_one(True, center_x)
    else:
      draw_one(False, center_x)

  HudRenderer._render = _render
  HudRenderer._nexo_med_hud_patched = True


class _HudPatchLoader(importlib.abc.Loader):
  def __init__(self, loader):
    self.loader = loader

  def create_module(self, spec):
    create = getattr(self.loader, "create_module", None)
    return create(spec) if create is not None else None

  def exec_module(self, module):
    self.loader.exec_module(module)
    _patch_hud_renderer(module)


class _HudPatchFinder(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path, target=None):
    if fullname != _TARGET:
      return None
    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
    if spec is None or spec.loader is None:
      return spec
    spec.loader = _HudPatchLoader(spec.loader)
    return spec


if _TARGET in sys.modules:
  _patch_hud_renderer(sys.modules[_TARGET])
elif not any(type(f).__name__ == "_HudPatchFinder" and type(f).__module__ == __name__ for f in sys.meta_path):
  sys.meta_path.insert(0, _HudPatchFinder())
