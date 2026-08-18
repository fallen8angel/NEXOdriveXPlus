import importlib.abc
import importlib.machinery
import sys
import time

import pyray as rl

SIDE_PANEL_WIDTH = 60
_NEXO_HUD_TARGET = "openpilot.selfdrive.ui.mici.onroad.hud_renderer"
_NEXO_MODEL_TARGET = "openpilot.selfdrive.ui.mici.onroad.model_renderer"
_NEXO_TURN_SIGNAL_BLINK_PERIOD = 1 / (80 / 60)  # match the stock Mici turn-signal cadence


def blend_colors(a: rl.Color, b: rl.Color, f: float) -> rl.Color:
  h0, s0, v0 = (hsv0 := rl.color_to_hsv(a)).x, hsv0.y, hsv0.z
  h1, s1, v1 = (hsv1 := rl.color_to_hsv(b)).x, hsv1.y, hsv1.z
  dh = ((h1 - h0 + 180) % 360) - 180  # shortest hue delta
  return rl.color_from_hsv((h0 + f * dh) % 360,
                           s0 + f * (s1 - s0),
                           v0 + f * (v1 - v0))


def _is_nexo(module) -> bool:
  cp = module.ui_state.CP
  fingerprint = getattr(cp, "carFingerprint", None) if cp is not None else None
  return getattr(fingerprint, "name", str(fingerprint)) == "HYUNDAI_NEXO_1ST_GEN"


def _patch_nexo_hud(module) -> None:
  """Keep the physical NEXO turn-signal icons, but do not draw blind-spot PNGs."""
  HudRenderer = module.HudRenderer
  if getattr(HudRenderer, "_nexo_turn_signal_hud_patched", False):
    return

  original_init = HudRenderer.__init__
  original_render = HudRenderer._render

  def _init(self):
    original_init(self)
    self._nexo_turn_signal_left = module.gui_app.texture('icons_mici/onroad/turn_signal_left.png', 104, 96)
    self._nexo_turn_signal_right = module.gui_app.texture('icons_mici/onroad/turn_signal_right.png', 104, 96)

  def _render(self, rect):
    original_render(self, rect)

    try:
      if not _is_nexo(module):
        return

      car_state = module.ui_state.sm["carState"]
      left_blinker = bool(car_state.leftBlinker)
      right_blinker = bool(car_state.rightBlinker)
    except Exception:
      return

    # Physical blinkers use the same 80 BPM cadence as the stock Mici alert renderer.
    blink_on = (time.monotonic() % _NEXO_TURN_SIGNAL_BLINK_PERIOD) < (_NEXO_TURN_SIGNAL_BLINK_PERIOD * 0.5)
    if not blink_on:
      return

    turn_margin_x = 2
    turn_margin_y = 5
    if left_blinker:
      module.rl.draw_texture(
        self._nexo_turn_signal_left,
        int(rect.x + turn_margin_x),
        int(rect.y + turn_margin_y),
        module.rl.WHITE,
      )
    if right_blinker:
      module.rl.draw_texture(
        self._nexo_turn_signal_right,
        int(rect.x + rect.width - self._nexo_turn_signal_right.width - turn_margin_x),
        int(rect.y + turn_margin_y),
        module.rl.WHITE,
      )

  HudRenderer.__init__ = _init
  HudRenderer._render = _render
  HudRenderer._nexo_turn_signal_hud_patched = True


def _patch_nexo_model(module) -> None:
  """Render NEXO BSM as OPKR-style red road areas instead of floating PNG icons."""
  ModelRenderer = module.ModelRenderer
  if getattr(ModelRenderer, "_nexo_opkr_blindspot_patched", False):
    return

  original_draw_lane_lines = ModelRenderer._draw_lane_lines

  def _build_blind_spot_area(self, lane_index: int, inner_shift: float, outer_shift: float):
    if len(self._lane_lines) <= lane_index or self._path.raw_points.shape[0] == 0:
      return module.np.empty((0, 2), dtype=module.np.float32)

    line = self._lane_lines[lane_index].raw_points
    if line.shape[0] == 0:
      return module.np.empty((0, 2), dtype=module.np.float32)

    max_distance = float(module.np.clip(
      self._path.raw_points[-1, 0],
      module.MIN_DRAW_DISTANCE,
      module.MAX_DRAW_DISTANCE,
    ))
    max_distance = min(max_distance, float(line[-1, 0]))
    max_idx = self._get_path_length_idx(line[:, 0], max_distance)

    outer_points = []
    inner_points = []
    for point in line[:max_idx + 1]:
      if float(point[0]) < 0.0:
        continue

      outer = self._map_to_screen(
        float(point[0]),
        float(point[1]) + outer_shift,
        float(point[2]),
      )
      inner = self._map_to_screen(
        float(point[0]),
        float(point[1]) + inner_shift,
        float(point[2]),
      )
      if outer is not None and inner is not None:
        outer_points.append(outer)
        inner_points.append(inner)

    if len(outer_points) < 2:
      return module.np.empty((0, 2), dtype=module.np.float32)

    return module.np.asarray(
      outer_points + list(reversed(inner_points)),
      dtype=module.np.float32,
    )

  def _draw_lane_lines(self):
    original_draw_lane_lines(self)

    try:
      if not _is_nexo(module):
        return

      car_state = module.ui_state.sm["carState"]
      left_blind_spot = bool(car_state.leftBlindspot)
      right_blind_spot = bool(car_state.rightBlindspot)
    except Exception:
      return

    if not (left_blind_spot or right_blind_spot):
      return

    # Match OPKR_NEXO update_blindspot_data(): each ego-lane boundary expands
    # 2.8 m outward and is filled red while the physical BSM state is active.
    warn_color = module.rl.Color(255, 0, 0, 190)

    if left_blind_spot:
      points = _build_blind_spot_area(self, 1, -0.01, -2.8)
      if points.size != 0:
        module.draw_polygon(self._rect, points, warn_color)

    if right_blind_spot:
      points = _build_blind_spot_area(self, 2, 0.01, 2.8)
      if points.size != 0:
        module.draw_polygon(self._rect, points, warn_color)

  ModelRenderer._draw_lane_lines = _draw_lane_lines
  ModelRenderer._nexo_opkr_blindspot_patched = True


_PATCHERS = {
  _NEXO_HUD_TARGET: _patch_nexo_hud,
  _NEXO_MODEL_TARGET: _patch_nexo_model,
}


class _NexoUiPatchLoader(importlib.abc.Loader):
  def __init__(self, fullname, loader):
    self.fullname = fullname
    self.loader = loader

  def create_module(self, spec):
    create = getattr(self.loader, "create_module", None)
    return create(spec) if create is not None else None

  def exec_module(self, module):
    self.loader.exec_module(module)
    _PATCHERS[self.fullname](module)


class _NexoUiPatchFinder(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path, target=None):
    if fullname not in _PATCHERS:
      return None

    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
    if spec is None or spec.loader is None:
      return spec

    spec.loader = _NexoUiPatchLoader(fullname, spec.loader)
    return spec


for _target, _patcher in _PATCHERS.items():
  if _target in sys.modules:
    _patcher(sys.modules[_target])

if not any(type(f).__name__ == "_NexoUiPatchFinder" and type(f).__module__ == __name__ for f in sys.meta_path):
  sys.meta_path.insert(0, _NexoUiPatchFinder())
