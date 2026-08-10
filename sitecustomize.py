"""NEXO-specific AI/MED compatibility layer.

Target behavior verified against NEXOdriveAI on-car logs:
  OFF -> MODE -> MED_WAIT (lateral only) -> SET/RES -> SPEED_CONTROL
      -> CANCEL -> MED_WAIT -> CANCEL -> OFF

The legacy NEXO AI fork does not rely only on the already-decoded buttonEvents.
It watches the raw CLU11 cruise-button deque and recreates button edges from it.
This compatibility layer mirrors that behavior for NEXO 1st gen only.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

NEXO_NAME = "HYUNDAI_NEXO_1ST_GEN"


def _is_nexo(value) -> bool:
  try:
    if getattr(value, "name", None) == NEXO_NAME:
      return True
  except Exception:
    pass
  text = str(value)
  return text == NEXO_NAME or text.endswith("." + NEXO_NAME)


def _patch_carstate(module) -> None:
  CarState = module.CarState
  if getattr(CarState, "_nexo_ai_med_patched", False):
    return

  original_update = CarState.update
  original_update_button_enable = CarState.update_button_enable
  ButtonType = module.ButtonType

  def _init_state(self):
    if hasattr(self, "_nexo_ai_available"):
      return
    self._nexo_ai_available = False
    self._nexo_ai_long_enabled = False
    self._nexo_ai_main_armed = False
    self._nexo_ai_main_zero_frames = 0
    self._nexo_ai_stock_main_prev = False
    self._nexo_ai_cancel_to_med = False
    self._nexo_ai_med_enable_pulse = False
    self._nexo_ai_prev_cruise_raw = None
    self.main_enabled = False

  def _raw_cruise_events(self):
    """Recreate AI-style CLU11 button edges from the raw cruise deque.

    NEXOdriveAI explicitly compares cruise_buttons[-1] against the previous raw
    value and creates press/release events.  Do the same here so SET/RES/CANCEL
    remains visible even when XPlus's normal buttonEvents path drops the event.
    """
    try:
      raw = int(self.cruise_buttons[-1])
    except Exception:
      return []

    prev = self._nexo_ai_prev_cruise_raw
    self._nexo_ai_prev_cruise_raw = raw
    if prev is None or raw == prev:
      return []

    try:
      return list(module.create_button_events(raw, prev, module.BUTTONS_DICT))
    except Exception:
      return []

  @staticmethod
  def _merge_events(decoded_events, raw_events):
    merged = []
    seen = set()
    for ev in list(decoded_events) + list(raw_events):
      try:
        key = (int(ev.type.raw), bool(ev.pressed))
      except Exception:
        try:
          key = (str(ev.type), bool(ev.pressed))
        except Exception:
          key = id(ev)
      if key in seen:
        continue
      seen.add(key)
      merged.append(ev)
    return merged

  def update(self, can_parsers):
    # Let XPlus decode the physical CAN first.  Its stock main_enabled toggle is
    # useful because the cluster already proves MODE itself is physically seen.
    ret = original_update(self, can_parsers)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return ret

    stock_main_enabled = bool(self.main_enabled)
    _init_state(self)

    # Ignore boot-time MODE noise until the physical main line has been released
    # for a few consecutive frames.
    try:
      main_raw = int(self.main_buttons[-1])
    except Exception:
      main_raw = 0

    if main_raw == 0:
      self._nexo_ai_main_zero_frames += 1
      if self._nexo_ai_main_zero_frames >= 3:
        self._nexo_ai_main_armed = True
    else:
      self._nexo_ai_main_zero_frames = 0

    # Source of truth for MODE is XPlus's decoded main_enabled transition.
    if self._nexo_ai_main_armed and stock_main_enabled != self._nexo_ai_stock_main_prev:
      was_available = self._nexo_ai_available
      self._nexo_ai_available = stock_main_enabled
      if self._nexo_ai_available and not was_available:
        self._nexo_ai_med_enable_pulse = True
      if not self._nexo_ai_available:
        self._nexo_ai_long_enabled = False
    self._nexo_ai_stock_main_prev = stock_main_enabled

    # AI-compatible raw CLU11 decoding. This is the critical difference from
    # the previous patch: SET/RES/CANCEL can now be reconstructed even when
    # ret.buttonEvents is empty.
    raw_events = _raw_cruise_events(self)
    input_events = _merge_events(ret.buttonEvents, raw_events)

    filtered_events = []
    for ev in input_events:
      typ = ev.type

      # Keep mainCruise as a diagnostic/UI event. State itself is synchronized
      # above from XPlus main_enabled so press/release representation cannot
      # break MED entry.
      if typ == ButtonType.mainCruise:
        if self._nexo_ai_main_armed:
          filtered_events.append(ev)
        continue

      if typ in (ButtonType.accelCruise, ButtonType.decelCruise):
        if not ev.pressed and self._nexo_ai_available:
          # Exact AI state transition: +/- release starts longitudinal control.
          self._nexo_ai_long_enabled = True
        # Important: pass the reconstructed event downstream too. selfdrived's
        # cruise-speed helper needs this event to create/change vCruise.
        filtered_events.append(ev)
        continue

      if typ == ButtonType.cancel:
        if ev.pressed and self._nexo_ai_long_enabled:
          # First CANCEL: SPEED_CONTROL -> MED_WAIT without dropping lateral.
          self._nexo_ai_long_enabled = False
          self._nexo_ai_cancel_to_med = True
          continue

        if not ev.pressed and self._nexo_ai_cancel_to_med:
          self._nexo_ai_cancel_to_med = False
          continue

        # CANCEL while already in MED_WAIT: OFF, matching AI.
        if not self._nexo_ai_long_enabled:
          self._nexo_ai_available = False
          self._nexo_ai_long_enabled = False
          self._nexo_ai_stock_main_prev = False
        filtered_events.append(ev)
        continue

      filtered_events.append(ev)

    ret.buttonEvents = filtered_events
    self.main_enabled = bool(self._nexo_ai_available)
    ret.cruiseState.available = bool(self._nexo_ai_available)
    ret.cruiseState.enabled = bool(self._nexo_ai_long_enabled)
    ret.cruiseState.standstill = False

    # MED_WAIT deliberately has no active target speed.  Once +/- is released,
    # the reconstructed button event is propagated so the normal XPlus cruise
    # speed logic can create the target, while cruiseState.enabled becomes true.
    if not self._nexo_ai_long_enabled:
      ret.cruiseState.speed = 0.0

    return ret

  def update_button_enable(self, button_events):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      _init_state(self)
      # One explicit enable pulse on OFF -> MED_WAIT.
      if self._nexo_ai_med_enable_pulse:
        self._nexo_ai_med_enable_pulse = False
        return True
      # For SET/RES, the normal implementation now receives the reconstructed
      # raw CLU11 event and can use its standard release-edge enable behavior.
    return original_update_button_enable(self, button_events)

  CarState.update = update
  CarState.update_button_enable = update_button_enable
  CarState._nexo_ai_med_patched = True


def _patch_controlsd(module) -> None:
  Controls = module.Controls
  if getattr(Controls, "_nexo_ai_med_patched", False):
    return

  original_state_control = Controls.state_control

  def state_control(self):
    CC, lac_log = original_state_control(self)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return CC, lac_log

    CS = self.sm["carState"]
    if not bool(CS.cruiseState.enabled):
      # MED_WAIT: global selfdrive/lateral can remain enabled, but longitudinal
      # actuation stays completely off until +/- selects a target speed.
      CC.longActive = False
      try:
        self.LoC.reset()
      except Exception:
        pass
      try:
        CC.actuators.accel = 0.0
        CC.actuators.aTarget = 0.0
        CC.actuators.jerk = 0.0
        CC.actuators.longControlState = module.car.CarControl.Actuators.LongControlState.off
      except Exception:
        pass
    return CC, lac_log

  Controls.state_control = state_control
  Controls._nexo_ai_med_patched = True


def _patch_hyundaican(module) -> None:
  if getattr(module, "_nexo_ai_med_patched", False):
    return

  original_scc = module.create_acc_commands_scc
  original_acc = module.create_acc_commands

  def create_acc_commands_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                              stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode):
    if _is_nexo(CS.CP.carFingerprint):
      enabled = bool(enabled and CS.out.cruiseState.enabled)
      if not enabled:
        accel = 0.0
        stopping = False
        long_override = False
    return original_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                        stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode)

  def create_acc_commands(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                          stopping, long_override, use_fca, CP, CS, soft_hold_mode):
    if _is_nexo(CP.carFingerprint):
      enabled = bool(enabled and CS.out.cruiseState.enabled)
      if not enabled:
        accel = 0.0
        stopping = False
        long_override = False
    return original_acc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                        stopping, long_override, use_fca, CP, CS, soft_hold_mode)

  module.create_acc_commands_scc = create_acc_commands_scc
  module.create_acc_commands = create_acc_commands
  module._nexo_ai_med_patched = True


_PATCHERS = {
  "opendbc.car.hyundai.carstate": _patch_carstate,
  "opendbc.car.hyundai.hyundaican": _patch_hyundaican,
  "openpilot.selfdrive.controls.controlsd": _patch_controlsd,
}


class _PatchLoader(importlib.abc.Loader):
  def __init__(self, loader, patcher):
    self.loader = loader
    self.patcher = patcher

  def create_module(self, spec):
    create = getattr(self.loader, "create_module", None)
    return create(spec) if create is not None else None

  def exec_module(self, module):
    self.loader.exec_module(module)
    self.patcher(module)


class _PatchFinder(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path, target=None):
    patcher = _PATCHERS.get(fullname)
    if patcher is None:
      return None
    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
    if spec is None or spec.loader is None:
      return spec
    spec.loader = _PatchLoader(spec.loader, patcher)
    return spec


if not any(type(f).__name__ == "_PatchFinder" and type(f).__module__ == __name__ for f in sys.meta_path):
  sys.meta_path.insert(0, _PatchFinder())
