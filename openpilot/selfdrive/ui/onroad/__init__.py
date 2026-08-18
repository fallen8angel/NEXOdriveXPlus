"""Onroad UI compatibility hooks for first-generation NEXO."""

from __future__ import annotations


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


def _patch_model_renderer(module) -> None:
  """Render NEXO blind-spot warnings as OPKR-style ground areas."""
  ModelRenderer = module.ModelRenderer
  if getattr(ModelRenderer, "_nexo_opkr_blindspot_patched", False):
    return

  def _draw_blind_spot_opkr(self, sm) -> None:
    input_services = ("modelV2", "carState", "radarState")
    if not all(sm.valid[service] for service in input_services):
      return

    car_state = sm["carState"]
    radar_state = sm["radarState"]
    meta = sm["modelV2"].meta

    left_blindspot, right_blindspot, left_assist, right_assist = self._blind_spot_draw_state_carrot(
      car_state, radar_state, meta,
    )
    if not (left_blindspot or right_blindspot or left_assist or right_assist):
      return

    if len(self._lane_lines) < 3 or self._path.raw_points.shape[0] == 0:
      return

    max_distance = float(module.np.clip(
      self._path.raw_points[-1, 0],
      module.MIN_DRAW_DISTANCE,
      module.MAX_DRAW_DISTANCE,
    ))

    warn_color = module.rl.Color(255, 0, 0, 190)
    assist_color = module.rl.Color(0, 204, 0, 150)

    def draw_area(lane_index: int, y_shift: float, color) -> None:
      line = self._lane_lines[lane_index].raw_points
      if line.shape[0] == 0:
        return

      line_max_distance = min(max_distance, float(line[-1, 0]))
      max_idx = self._get_path_length_idx(line[:, 0], line_max_distance)

      # OPKR_NEXO expands each ego-lane boundary 2.8 m outward on the road
      # surface. A 1.4 m half-width plus a +/-1.4 m center shift reproduces
      # that asymmetric 0..2.8 m blind-spot area with the XPlus projector.
      points = self._map_line_to_polygon(
        line,
        1.4,
        0.0,
        max_idx,
        line_max_distance,
        True,
        y_shift,
      )
      if points.size != 0:
        module.draw_polygon_solid(points, color)

    if left_blindspot:
      draw_area(1, -1.4, warn_color)
    elif left_assist:
      draw_area(1, -1.4, assist_color)

    if right_blindspot:
      draw_area(2, 1.4, warn_color)
    elif right_assist:
      draw_area(2, 1.4, assist_color)

  ModelRenderer._draw_blind_spot_carrot = _draw_blind_spot_opkr
  ModelRenderer._nexo_opkr_blindspot_patched = True


# Apply the patches eagerly to the actual renderers instead of relying on a
# meta import hook. openpilot.selfdrive.ui imports this package during UI
# startup, so the real renderer classes are patched before the onroad widget
# is created.
try:
  from openpilot.selfdrive.ui.onroad import hud_renderer as _hud_renderer
  _patch_hud_renderer(_hud_renderer)
except Exception as e:
  print(f"NEXO HUD patch failed: {e}")

try:
  from openpilot.selfdrive.ui.onroad import model_renderer as _model_renderer
  _patch_model_renderer(_model_renderer)
except Exception as e:
  print(f"NEXO model renderer patch failed: {e}")
