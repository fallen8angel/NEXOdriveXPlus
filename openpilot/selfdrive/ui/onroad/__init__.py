"""Onroad UI compatibility hooks."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys


_TARGET = "openpilot.selfdrive.ui.onroad.hud_renderer"


def _patch_hud_renderer(module) -> None:
  HudRenderer = module.HudRenderer
  if getattr(HudRenderer, "_nexo_persistent_lane_arrow_patched", False):
    return

  original_render = HudRenderer._render

  def _render(self, rect):
    original_render(self, rect)

    # Show lane-change arrows from the real vehicle blinker state, independent
    # of cruise/MED/openpilot engagement and independent of Carrot navigation
    # xTurnInfo. Keep the arrow visible for as long as the blinker state is on.
    try:
      car_state = module.ui_state.sm["carState"]
      left = bool(car_state.leftBlinker)
      right = bool(car_state.rightBlinker)
    except Exception:
      return

    if not left and not right:
      return

    icon_size = 180
    center_x = float(rect.x + rect.width / 2.0)
    center_y = float(rect.y + rect.height * 0.58)

    if left and right:
      offset = 120.0
      self._draw_texture_rect(self._ic_lane_change_l,
                              center_x - offset - icon_size / 2.0,
                              center_y - icon_size / 2.0,
                              icon_size, icon_size)
      self._draw_texture_rect(self._ic_lane_change_r,
                              center_x + offset - icon_size / 2.0,
                              center_y - icon_size / 2.0,
                              icon_size, icon_size)
    elif left:
      self._draw_texture_rect(self._ic_lane_change_l,
                              center_x - icon_size / 2.0,
                              center_y - icon_size / 2.0,
                              icon_size, icon_size)
    else:
      self._draw_texture_rect(self._ic_lane_change_r,
                              center_x - icon_size / 2.0,
                              center_y - icon_size / 2.0,
                              icon_size, icon_size)

  HudRenderer._render = _render
  HudRenderer._nexo_persistent_lane_arrow_patched = True


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
