"""NEXO-specific AI/MED compatibility layer.

This module is loaded automatically by Python's site module.  It installs
small post-import patches only for the NEXO 1st gen so the rest of the
Hyundai/Carrot stack stays unchanged.

Target behavior, verified against NEXOdriveAI on-car logs:
  OFF -> MODE -> MED_WAIT (lateral only) -> SET/RES -> SPEED_CONTROL
      -> CANCEL -> MED_WAIT -> CANCEL -> OFF
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
    self._nexo_ai_prev_main_raw = 0
    self._nexo_ai_cancel_to_med = False
    self.main_enabled = False

  def update(self, can_parsers):
    ret = original_update(self, can_parsers)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return ret

    _init_state(self)

    # Reject the boot-time CLU11 transient. MODE handling below is event-driven,
    # but we do not trust a mainCruise event until the physical main line has
    # first been observed released for a few consecutive frames.
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
    self._nexo_ai_prev_main_raw = main_raw

    filtered_events = []
    for ev in list(ret.buttonEvents):
      typ = ev.type

      if typ == ButtonType.mainCruise:
        if not self._nexo_ai_main_armed:
          continue
        # Use the decoded MODE/mainCruise event itself as the source of truth.
        # XPlus/NEXO can change the stock cluster on MODE even when the sampled
        # raw main deque does not present a clean rising edge to this wrapper.
        if ev.pressed:
          self._nexo_ai_available = not self._nexo_ai_available
          if not self._nexo_ai_available:
            self._nexo_ai_long_enabled = False
        filtered_events.append(ev)
        continue

      if typ in (ButtonType.accelCruise, ButtonType.decelCruise):
        if not ev.pressed and self._nexo_ai_available:
          self._nexo_ai_long_enabled = True
        filtered_events.append(ev)
        continue

      if typ == ButtonType.cancel:
        if ev.pressed:
          if self._nexo_ai_long_enabled:
            # First CANCEL: SPEED_CONTROL -> MED_WAIT. Consume both edges so
            # selfdrived does not fully disengage lateral control.
            self._nexo_ai_long_enabled = False
            self._nexo_ai_cancel_to_med = True
            continue
          filtered_events.append(ev)
          continue

        if self._nexo_ai_cancel_to_med:
          self._nexo_ai_cancel_to_med = False
          continue
        if self._nexo_ai_long_enabled:
          self._nexo_ai_long_enabled = False
          continue

        # Second CANCEL while already in MED_WAIT: OFF.
        self._nexo_ai_available = False
        self._nexo_ai_long_enabled = False
        filtered_events.append(ev)
        continue

      filtered_events.append(ev)

    ret.buttonEvents = filtered_events
    self.main_enabled = bool(self._nexo_ai_available)
    ret.cruiseState.available = bool(self._nexo_ai_available)
    ret.cruiseState.enabled = bool(self._nexo_ai_long_enabled)
    ret.cruiseState.standstill = False

    if not self._nexo_ai_long_enabled:
      ret.cruiseState.speed = 0.0

    return ret

  def update_button_enable(self, button_events):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      _init_state(self)
      for ev in button_events:
        # MODE release engages openpilot into MED_WAIT. SET/RES remains the
        # transition that enables longitudinal speed control.
        if ev.type == ButtonType.mainCruise and not ev.pressed and self._nexo_ai_available:
          return True
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
      # MED_WAIT: keep the global/openpilot lateral engagement but prohibit
      # longitudinal actuation until a SET/RES release selects a target speed.
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
