"""AI-style cruise state manager for Hyundai NEXO 1st gen.

This is intentionally self-contained and only used by sitecustomize.py for
HYUNDAI_NEXO_1ST_GEN. It mirrors the legacy NEXOdriveAI state machine:

  OFF -> MODE -> MED_WAIT -> SET/RES -> SPEED_CONTROL
      -> CANCEL -> MED_WAIT -> CANCEL -> OFF

Unlike the previous compatibility patches, available/enabled/target speed and
physical CLU11 button state are owned here continuously so XPlus's independent
cruise helpers cannot leave NEXO half-engaged or lose repeated +/- presses.
"""
from __future__ import annotations


class NexoAICruiseStateManager:
  MIN_SPEED_KPH = 10.0
  DEFAULT_SPEED_KPH = 30.0
  MAX_SPEED_KPH = 160.0
  LONG_PRESS_FRAMES = 70
  MAIN_RELEASE_ARM_FRAMES = 3

  def __init__(self, button_type, buttons_dict, create_button_events, kph_to_ms: float, mph_to_kph: float):
    self.ButtonType = button_type
    self.buttons_dict = buttons_dict
    self.create_button_events = create_button_events
    self.KPH_TO_MS = float(kph_to_ms)
    self.MPH_TO_KPH = float(mph_to_kph)

    self.available = False
    self.enabled = False
    self.speed_kph = self.DEFAULT_SPEED_KPH

    self.main_armed = False
    self.main_release_frames = 0
    self.prev_stock_main = False
    self.enable_pulse = False

    self.prev_raw_button = 0
    self.held_button = 0
    self.held_frames = 0
    self.long_press_fired = False
    self.prev_brake_pressed = False

  @staticmethod
  def _clip(value, lo, hi):
    return max(lo, min(hi, float(value)))

  def _current_speed_kph(self, car_state) -> float:
    try:
      return max(0.0, float(car_state.vEgoCluster) / self.KPH_TO_MS)
    except Exception:
      try:
        return max(0.0, float(car_state.vEgo) / self.KPH_TO_MS)
      except Exception:
        return 0.0

  def _step_kph(self, is_metric: bool, long_press: bool) -> float:
    if long_press:
      return 10.0 if is_metric else 5.0 * self.MPH_TO_KPH
    return 1.0 if is_metric else self.MPH_TO_KPH

  def _apply_speed_button(self, car_state, button_type, is_metric: bool, long_press: bool = False) -> None:
    if not self.available:
      return

    current_kph = self._current_speed_kph(car_state)
    step = self._step_kph(is_metric, long_press)

    if not self.enabled:
      # First SET/- captures current speed. First RES/+ resumes the retained
      # target but never below current speed, matching legacy NEXOdriveAI.
      if button_type == self.ButtonType.decelCruise:
        self.speed_kph = self._clip(max(current_kph, self.MIN_SPEED_KPH), self.MIN_SPEED_KPH, self.MAX_SPEED_KPH)
        self.enabled = True
      elif button_type == self.ButtonType.accelCruise:
        self.speed_kph = self._clip(max(self.speed_kph, current_kph, self.MIN_SPEED_KPH), self.MIN_SPEED_KPH, self.MAX_SPEED_KPH)
        self.enabled = True
      return

    if button_type == self.ButtonType.accelCruise:
      if long_press:
        self.speed_kph += step - (self.speed_kph % step)
      else:
        self.speed_kph += step
    elif button_type == self.ButtonType.decelCruise:
      if long_press:
        rem = self.speed_kph % step
        self.speed_kph -= rem if rem > 0.01 else step
      else:
        self.speed_kph -= step

    self.speed_kph = self._clip(self.speed_kph, self.MIN_SPEED_KPH, self.MAX_SPEED_KPH)

  def _handle_cancel(self) -> None:
    if self.enabled:
      # First CANCEL: SPEED_CONTROL -> MED_WAIT.
      self.enabled = False
    else:
      # Second CANCEL while already MED: complete OFF.
      self.available = False
      self.enabled = False
      self.prev_stock_main = False

  def _handle_release(self, car_state, raw_button: int, is_metric: bool) -> None:
    try:
      events = list(self.create_button_events(0, raw_button, self.buttons_dict))
    except Exception:
      events = []
    for ev in events:
      if bool(ev.pressed):
        continue
      if ev.type in (self.ButtonType.accelCruise, self.ButtonType.decelCruise):
        if not self.long_press_fired:
          self._apply_speed_button(car_state, ev.type, is_metric, False)
      elif ev.type == self.ButtonType.cancel:
        self._handle_cancel()

  def update(self, car_state, raw_main: int, raw_button: int, stock_main_enabled: bool,
             is_metric: bool, decoded_events) -> list:
    """Update state and return physical button events for downstream consumers."""
    raw_main = int(raw_main)
    raw_button = int(raw_button)

    # Ignore CLU11 startup transients until the physical MODE line is stably
    # released. After that XPlus's decoded main_enabled transition is the source
    # of truth for MODE.
    if raw_main == 0:
      self.main_release_frames += 1
      if self.main_release_frames >= self.MAIN_RELEASE_ARM_FRAMES:
        self.main_armed = True
    else:
      self.main_release_frames = 0

    if self.main_armed and bool(stock_main_enabled) != self.prev_stock_main:
      new_available = bool(stock_main_enabled)
      if new_available and not self.available:
        self.available = True
        self.enabled = False
        self.enable_pulse = True
      elif not new_available:
        self.available = False
        self.enabled = False
      self.prev_stock_main = bool(stock_main_enabled)

    # Recreate physical CLU11 edges exactly from raw state changes, as the AI
    # fork does. Every edge is also passed downstream so repeated +/- presses
    # remain visible to selfdrive/UI.
    try:
      raw_events = list(self.create_button_events(raw_button, self.prev_raw_button, self.buttons_dict))
    except Exception:
      raw_events = []

    if raw_button != self.prev_raw_button:
      old_button = self.prev_raw_button

      # One and only one release action for the old button. This covers normal
      # nonzero->0 release and direct nonzero->different-nonzero transitions.
      if old_button != 0:
        self._handle_release(car_state, old_button, is_metric)

      if raw_button == 0:
        self.held_button = 0
        self.held_frames = 0
        self.long_press_fired = False
      else:
        self.held_button = raw_button
        self.held_frames = 1
        self.long_press_fired = False

    elif raw_button != 0:
      self.held_frames += 1
      if self.held_frames > self.LONG_PRESS_FRAMES and self.held_frames % self.LONG_PRESS_FRAMES == 1:
        try:
          press_events = list(self.create_button_events(raw_button, 0, self.buttons_dict))
        except Exception:
          press_events = []
        for ev in press_events:
          if ev.pressed and ev.type in (self.ButtonType.accelCruise, self.ButtonType.decelCruise):
            self._apply_speed_button(car_state, ev.type, is_metric, True)
            self.long_press_fired = True

    self.prev_raw_button = raw_button

    # Braking returns longitudinal control to MED without losing MODE/lateral.
    brake_pressed = bool(getattr(car_state, "brakePressed", False))
    if brake_pressed and not self.prev_brake_pressed:
      self.enabled = False
    self.prev_brake_pressed = brake_pressed

    merged = []
    seen = set()
    for ev in list(decoded_events) + raw_events:
      try:
        key = (str(ev.type), bool(ev.pressed))
      except Exception:
        key = id(ev)
      if key in seen:
        continue
      seen.add(key)
      merged.append(ev)

    return merged

  def consume_enable_pulse(self) -> bool:
    pulse = self.enable_pulse
    self.enable_pulse = False
    return pulse

  def apply_to_car_state(self, car_state) -> None:
    car_state.cruiseState.available = bool(self.available)
    car_state.cruiseState.enabled = bool(self.enabled)
    car_state.cruiseState.standstill = False
    car_state.cruiseState.speed = (self.speed_kph * self.KPH_TO_MS) if self.enabled else 0.0

  @property
  def speed_ms(self) -> float:
    return self.speed_kph * self.KPH_TO_MS
