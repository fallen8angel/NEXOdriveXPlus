import time
import pyray as rl
from openpilot.selfdrive.ui.mici.onroad import SIDE_PANEL_WIDTH
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.carrot.cluster.signal_display import live_signal, nav_phase
from openpilot.system.ui.widgets import Widget
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.ui.mici.onroad.confidence_ball import draw_circle_gradient


class TrafficLight(Widget):
  def __init__(self):
    super().__init__()
    self._radius = 24
    self._current_state = 0
    self._stopped = False
    self._green_start_time = None

    # fade animation (0~1)
    self._alpha_filter = FirstOrderFilter(0.0, 1.0, 1/60.0)

  # --------------------------------------------------

  def is_visible(self):
    return self._alpha_filter.x > 0.05

  # --------------------------------------------------

  def _update_state(self):
    now = time.monotonic()
    nav = nav_phase(ui_state.params_memory.get("TrafficLight"), now)
    state, self._stopped = live_signal(ui_state.sm, now, nav)

    visible = False

    if state in (1, 3):
      # red always visible
      visible = True
      self._current_state = state
      self._green_start_time = None

    elif state == 2:
      # green max 2 sec
      if self._current_state != 2:
        self._green_start_time = time.monotonic()

      self._current_state = 2

      if self._green_start_time and (time.monotonic() - self._green_start_time <= 2.0):
        visible = True
      else:
        visible = False

    else:
      visible = False
      self._current_state = 0
      self._green_start_time = None

    # fade target
    self._alpha_filter.update(1.0 if visible else 0.0)

  # --------------------------------------------------

  def _render(self, _):
    alpha = max(0.0, min(1.0, self._alpha_filter.x))
    if alpha <= 0.01 or not self._current_state:
      return

    content_rect = rl.Rectangle(
      self.rect.x + self.rect.width - SIDE_PANEL_WIDTH,
      self.rect.y,
      SIDE_PANEL_WIDTH,
      self.rect.height,
    )

    center_x = content_rect.x + content_rect.width - self._radius
    center_y = self.rect.y + self._radius

    # 
    if self._current_state == 1:
      top = rl.Color(255, 80, 80, int(255 * alpha))
      bottom = rl.Color(255, 0, 0, int(255 * alpha))
    elif self._current_state == 3:
      top = bottom = rl.Color(255, 183, 30, int(255 * alpha))
    else:
      top = rl.Color(120, 255, 120, int(255 * alpha))
      bottom = rl.Color(0, 255, 0, int(255 * alpha))

    draw_circle_gradient(center_x, center_y, self._radius, top, bottom)
    if self._stopped:
      rl.draw_text("STOP", int(center_x - 16), int(center_y - 7), 14, rl.BLACK)
