import pyray as rl
import openpilot.cereal.messaging as messaging
from openpilot.cereal import car
from openpilot.selfdrive.ui.mici.layouts.home import MiciHomeLayout
from openpilot.selfdrive.ui.mici.layouts.settings.settings import SettingsLayout
from openpilot.selfdrive.ui.mici.layouts.offroad_alerts import MiciOffroadAlerts
from openpilot.selfdrive.ui.mici.onroad.augmented_road_view import AugmentedRoadView
from openpilot.selfdrive.ui.ui_state import device, ui_state
from openpilot.selfdrive.ui.widgets.carrot_web_dialog import CarrotWebDialog
from openpilot.selfdrive.ui.mici.layouts.onboarding import OnboardingWindow
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.scroller import Scroller
from openpilot.system.ui.lib.application import gui_app
from openpilot.selfdrive.ui.mici.onroad.debug_plot import DebugPlot
from openpilot.selfdrive.ui.mici.onroad.driver_camera_dialog import DriverCameraDialog
from openpilot.selfdrive.ui.mici.reverse_camera_state import reverse_camera_action, should_show_reverse_camera

ONROAD_DELAY = 2.5  # seconds


class MiciMainLayout(Scroller):
  def __init__(self):
    super().__init__(snap_items=True, spacing=0, pad=0, scroll_indicator=False, edge_shadows=False)

    self._pm = messaging.PubMaster(['bookmarkButton'])

    self._prev_onroad = False
    self._prev_standstill = False
    self._onroad_time_delay: float | None = None
    self._setup = False

    # Initialize widgets
    self._home_layout = MiciHomeLayout()
    self._alerts_layout = MiciOffroadAlerts()
    self._settings_layout = SettingsLayout()
    self._onroad_layout = AugmentedRoadView(bookmark_callback=self._on_bookmark_clicked)
    self._debug_layout = DebugPlot()
    self._show_plot_mode = 0
    self._in_plot_mode = False
    self._reverse_driver_camera_dialog: DriverCameraDialog | None = None
    self._reverse_driver_camera_closing = False
    self._reverse_driver_camera_requested = False
    self._reverse_driver_camera_migration_checked = False
    self._reverse_driver_camera_migration_pending = False

    # Initialize widget rects
    for widget in (self._home_layout, self._settings_layout, self._alerts_layout, self._onroad_layout, self._debug_layout):
      # TODO: set parent rect and use it if never passed rect from render (like in Scroller)
      widget.set_rect(rl.Rectangle(0, 0, gui_app.width, gui_app.height))

    self._scroller.add_widgets([
      self._alerts_layout,
      self._home_layout,
      self._onroad_layout,
      self._debug_layout,
    ])
    self._scroller.set_reset_scroll_at_show(False)

    # Disable scrolling when onroad is interacting with bookmark
    self._scroller.set_scrolling_enabled(lambda: not self._onroad_layout.is_swiping_left())

    # Set callbacks
    self._setup_callbacks()

    gui_app.add_nav_stack_tick(self._handle_transitions)
    gui_app.push_widget(self)

    # Start onboarding if terms or training not completed, make sure to push after self
    self._onboarding_window = OnboardingWindow(lambda: gui_app.pop_widgets_to(self))
    if not self._onboarding_window.completed:
      gui_app.push_widget(self._onboarding_window)

    # carrot_man
    self._last_carrot_cmd_idx = -1

  @staticmethod
  def _sync_screen_record_state(requested: bool) -> bool:
    recording = gui_app.is_recording()
    if requested != recording:
      ui_state.params.put_bool_nonblocking("ScreenRecord", recording)
    return recording

  def _handle_carrot_record_cmd(self, sm) -> bool:
    screen_record = ui_state.params.get_bool("ScreenRecord")
    if screen_record:
      gui_app.start_recording()
    else:
      gui_app.stop_recording()
    recording = self._sync_screen_record_state(screen_record)

    try:
      cm = sm['carrotMan']
      cmd_idx = int(cm.carrotCmdIndex)
      cmd = str(cm.carrotCmd)
      arg = str(cm.carrotArg)
    except Exception as e:
      print(f"Error reading carrotMan message: {e}")
      return recording

    if cmd_idx == self._last_carrot_cmd_idx or self._last_carrot_cmd_idx == -1:
      self._last_carrot_cmd_idx = cmd_idx
      return recording
    print(f"CarrotMan command received: {cmd} {arg} (index {cmd_idx})")
    self._last_carrot_cmd_idx = cmd_idx

    if not ui_state.started:
      gui_app.stop_recording()
      return self._sync_screen_record_state(screen_record)


    if cmd != "RECORD":
      return recording

    arg = arg.upper()
    if arg == "START":
      gui_app.start_recording()
    elif arg == "STOP":
      gui_app.stop_recording()
    elif arg == "TOGGLE":
      gui_app.toggle_recording()

    return self._sync_screen_record_state(screen_record)

  def _setup_callbacks(self):
    self._home_layout.set_callbacks(
      on_settings=lambda: gui_app.push_widget(self._settings_layout),
      on_carrot_web=lambda: gui_app.push_widget(CarrotWebDialog()),
    )
    self._onroad_layout.set_click_callback(lambda: self._scroll_to(self._home_layout))
    device.add_interactive_timeout_callback(self._on_interactive_timeout)

  def _scroll_to(self, layout: Widget):
    target_is_plot  = (layout is self._debug_layout)
    if not target_is_plot  and self._in_plot_mode:
      return
    self._in_plot_mode = target_is_plot
    layout_x = int(layout.rect.x)
    self._scroller.scroll_to(layout_x, smooth=True)

  def _render(self, _):
    if not self._setup:
      if self._alerts_layout.active_alerts() > 0:
        self._scroller.scroll_to(self._alerts_layout.rect.x)
      else:
        self._scroller.scroll_to(self._rect.width)
      self._setup = True

    # Render
    super()._render(self._rect)

    self._handle_carrot_record_cmd(ui_state.sm)

  def _reverse_camera_enabled(self) -> bool:
    enabled = ui_state.params.get_bool("ReverseDriverCamera")

    if not self._reverse_driver_camera_migration_checked:
      cp = ui_state.CP
      if cp is None:
        return enabled

      fingerprint = getattr(cp, "carFingerprint", None)
      is_nexo = getattr(fingerprint, "name", str(fingerprint)) == "HYUNDAI_NEXO_1ST_GEN"
      self._reverse_driver_camera_migration_checked = True

      if is_nexo and not ui_state.params.get_bool("ReverseDriverCameraNexoMigrated"):
        ui_state.params.put_bool_nonblocking("ReverseDriverCamera", True)
        ui_state.params.put_bool_nonblocking("ReverseDriverCameraNexoMigrated", True)
        self._reverse_driver_camera_migration_pending = True
        return True

    if self._reverse_driver_camera_migration_pending:
      if enabled:
        self._reverse_driver_camera_migration_pending = False
      else:
        return True

    return enabled

  def _handle_transitions(self):
    if gui_app.widget_in_stack(self._onboarding_window):
      return

    CS = ui_state.sm["carState"]
    self._reverse_driver_camera_requested = should_show_reverse_camera(
      self._reverse_camera_enabled(), ui_state.started,
      CS.gearShifter == car.CarState.GearShifter.reverse,
    )
    reverse_camera_active = (self._reverse_driver_camera_requested or self._reverse_driver_camera_closing or
                             self._reverse_driver_camera_dialog is not None)

    if ui_state.started != self._prev_onroad:
      self._prev_onroad = ui_state.started
      if ui_state.started:
        self._onroad_time_delay = rl.get_time()
      elif not reverse_camera_active:
        self._scroll_to(self._home_layout)

    if (not reverse_camera_active and self._onroad_time_delay is not None and
        rl.get_time() - self._onroad_time_delay >= ONROAD_DELAY):
      gui_app.pop_widgets_to(self, lambda: self._scroll_to(self._onroad_layout))
      self._onroad_time_delay = None

    if ui_state.started and not reverse_camera_active:
      show_plot_mode = ui_state.params.get_int("ShowPlotMode")
      cluster_hud_connected = ui_state.params.get_bool("ClusterHudConnected")
      self._onroad_layout.set_cluster_hud_connected(cluster_hud_connected, ui_state.show_camera_with_cluster)
      effective_plot_mode = 0 if cluster_hud_connected else show_plot_mode
      if effective_plot_mode != self._show_plot_mode:
        self._show_plot_mode = effective_plot_mode
        if self._show_plot_mode > 0:
          self._scroll_to(self._debug_layout)
        else:
          self._in_plot_mode = False
          self._scroll_to(self._onroad_layout)

    if not reverse_camera_active and not CS.standstill and self._prev_standstill:
      gui_app.pop_widgets_to(self, lambda: self._scroll_to(self._onroad_layout))
    self._prev_standstill = CS.standstill

    action = reverse_camera_action(
      self._reverse_driver_camera_requested,
      self._reverse_driver_camera_dialog is not None,
      self._reverse_driver_camera_dialog is not None and gui_app.widget_in_stack(self._reverse_driver_camera_dialog),
      self._reverse_driver_camera_closing,
    )
    if action == "create":
      self._reverse_driver_camera_dialog = DriverCameraDialog(close_on_timeout=False, show_dm_overlay=False)
      gui_app.push_widget(self._reverse_driver_camera_dialog)
    elif action == "push":
      gui_app.push_widget(self._reverse_driver_camera_dialog)
    elif action == "dismiss":
      self._reverse_driver_camera_closing = True
      gui_app.pop_widgets_to(self, self._finish_reverse_camera)
    elif action == "close":
      self._finish_reverse_camera()

  def _finish_reverse_camera(self) -> None:
    self._reverse_driver_camera_closing = False
    dialog = self._reverse_driver_camera_dialog
    if dialog is None:
      return
    if self._reverse_driver_camera_requested:
      if not gui_app.widget_in_stack(dialog):
        gui_app.push_widget(dialog)
      return

    self._reverse_driver_camera_dialog = None
    dialog.close()
    if ui_state.started:
      self._in_plot_mode = False
      self._scroll_to(self._onroad_layout)
    else:
      self._scroll_to(self._home_layout)

  def _on_interactive_timeout(self):
    # Don't pop if onboarding
    if gui_app.widget_in_stack(self._onboarding_window):
      return
    if self._reverse_driver_camera_requested or self._reverse_driver_camera_closing:
      return

    if ui_state.started:
      # Don't pop if at standstill
      if not ui_state.sm["carState"].standstill:
        gui_app.pop_widgets_to(self, lambda: self._scroll_to(self._onroad_layout))
    else:
      # Screen turns off on timeout offroad, so pop immediately without animation
      gui_app.pop_widgets_to(self, instant=True)
      self._scroll_to(self._home_layout)

  def _on_bookmark_clicked(self):
    user_bookmark = messaging.new_message('bookmarkButton')
    user_bookmark.valid = True
    self._pm.send('bookmarkButton', user_bookmark)
