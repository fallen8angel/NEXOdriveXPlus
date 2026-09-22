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
  """Render NEXO BSM as a clipped red adjacent-lane floor area, matching the external HUD."""
  ModelRenderer = module.ModelRenderer
  if getattr(ModelRenderer, "_nexo_opkr_blindspot_patched", False):
    return

  original_draw_lane_lines = ModelRenderer._draw_lane_lines

  def _project_blind_spot_point(self, point, lateral_shift: float):
    input_pt = module.np.asarray(
      (float(point[0]), float(point[1]) + lateral_shift, float(point[2])),
      dtype=module.np.float32,
    )
    projected = self._car_space_transform @ input_pt
    depth = float(projected[2])
    if abs(depth) < 1e-6:
      return None

    x = float(projected[0] / depth)
    y = float(projected[1] / depth)
    if not (module.np.isfinite(x) and module.np.isfinite(y)):
      return None
    return (x, y)

  def _clip_polygon_to_view(self, points, side: int):
    """Clip BSM floor to the real Mici viewport and its matching screen half."""
    if len(points) < 3:
      return module.np.empty((0, 2), dtype=module.np.float32)

    # Use the visible rect itself rather than the renderer's expanded clip
    # margin. The expanded margin can turn an off-screen lane projection into
    # a very large triangle when it is later filled.
    rect = self._rect
    x_min = float(rect.x)
    x_max = float(rect.x + rect.width)
    y_min = float(rect.y + rect.height * 0.20)
    y_max = float(rect.y + rect.height)
    center_x = float(rect.x + rect.width * 0.5)

    # A physical left BSM warning must stay on the left side of the Comma
    # display, and vice versa. This prevents a curved lane from crossing the
    # screen center and creating a giant self-intersecting fill.
    if side < 0:
      x_max = center_x
    elif side > 0:
      x_min = center_x

    def clip_edge(poly, inside, intersect):
      if not poly:
        return []
      out = []
      prev = poly[-1]
      prev_inside = inside(prev)
      for cur_pt in poly:
        cur_inside = inside(cur_pt)
        if cur_inside:
          if not prev_inside:
            out.append(intersect(prev, cur_pt))
          out.append(cur_pt)
        elif prev_inside:
          out.append(intersect(prev, cur_pt))
        prev = cur_pt
        prev_inside = cur_inside
      return out

    def intersect_x(a, b, x):
      dx = b[0] - a[0]
      if abs(dx) < 1e-6:
        return (x, a[1])
      t = (x - a[0]) / dx
      return (x, a[1] + t * (b[1] - a[1]))

    def intersect_y(a, b, y):
      dy = b[1] - a[1]
      if abs(dy) < 1e-6:
        return (a[0], y)
      t = (y - a[1]) / dy
      return (a[0] + t * (b[0] - a[0]), y)

    poly = list(points)
    poly = clip_edge(poly, lambda p: p[0] >= x_min, lambda a, b: intersect_x(a, b, x_min))
    poly = clip_edge(poly, lambda p: p[0] <= x_max, lambda a, b: intersect_x(a, b, x_max))
    poly = clip_edge(poly, lambda p: p[1] >= y_min, lambda a, b: intersect_y(a, b, y_min))
    poly = clip_edge(poly, lambda p: p[1] <= y_max, lambda a, b: intersect_y(a, b, y_max))

    if len(poly) < 3:
      return module.np.empty((0, 2), dtype=module.np.float32)
    return module.np.asarray(poly, dtype=module.np.float32)

  def _blind_spot_floor_from_line(self, line, inner_shift: float, outer_shift: float, side: int):
    if line.shape[0] == 0 or self._path.raw_points.shape[0] == 0:
      return module.np.empty((0, 2), dtype=module.np.float32)

    # Blind-spot indication is a near-vehicle warning. Limiting the fill to
    # 32 m avoids perspective collapse near the horizon, which was responsible
    # for the oversized left-side red wedge seen on the Mici display.
    max_distance = min(
      32.0,
      float(line[-1, 0]),
      float(module.np.clip(
        self._path.raw_points[-1, 0],
        module.MIN_DRAW_DISTANCE,
        module.MAX_DRAW_DISTANCE,
      )),
    )
    max_idx = self._get_path_length_idx(line[:, 0], max_distance)

    inner_points = []
    outer_points = []
    for point in line[:max_idx + 1]:
      if float(point[0]) < 0.0:
        continue
      inner = _project_blind_spot_point(self, point, inner_shift)
      outer = _project_blind_spot_point(self, point, outer_shift)
      if inner is not None and outer is not None:
        inner_points.append(inner)
        outer_points.append(outer)

    if len(inner_points) < 2:
      return module.np.empty((0, 2), dtype=module.np.float32)

    polygon = inner_points + list(reversed(outer_points))
    return _clip_polygon_to_view(self, polygon, side)

  def _build_blind_spot_floor(self, lane_index: int, side: int):
    """Fill the adjacent lane floor from the ego-lane boundary outward by 2.8 m."""
    if self._path.raw_points.shape[0] == 0:
      return module.np.empty((0, 2), dtype=module.np.float32)

    inner_shift = side * 0.01
    outer_shift = side * 2.8

    # Prefer the real model ego-lane boundary, just like the external HUD.
    if 0 <= lane_index < len(self._lane_lines):
      lane = self._lane_lines[lane_index].raw_points
      points = _blind_spot_floor_from_line(self, lane, inner_shift, outer_shift, side)
      if points.size != 0:
        return points

    # If the boundary briefly disappears, keep the warning stable by using
    # the model path with a nominal 3.6 m ego-lane width.
    boundary_shift = side * 1.8
    return _blind_spot_floor_from_line(
      self._path.raw_points,
      boundary_shift + inner_shift,
      boundary_shift + outer_shift,
      side,
    )

  def _draw_lane_lines(self):
    try:
      is_nexo = _is_nexo(module)
      car_state = module.ui_state.sm["carState"]
      left_blind_spot = is_nexo and bool(car_state.leftBlindspot)
      right_blind_spot = is_nexo and bool(car_state.rightBlindspot)
    except Exception:
      left_blind_spot = False
      right_blind_spot = False

    # Draw the warning floor first so the normal lane markings remain visible
    # on top. This matches the external HUD presentation and avoids the old
    # oversized red triangle caused by unbounded off-screen projection.
    warn_color = module.rl.Color(255, 0, 0, 175)
    if left_blind_spot:
      points = _build_blind_spot_floor(self, 1, -1)
      if points.size != 0:
        module.draw_polygon(self._rect, points, warn_color)
    if right_blind_spot:
      points = _build_blind_spot_floor(self, 2, 1)
      if points.size != 0:
        module.draw_polygon(self._rect, points, warn_color)

    original_draw_lane_lines(self)

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
