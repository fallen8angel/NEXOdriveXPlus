"""NEXO 1st-gen AI/MED compatibility integration.

OFF -> MODE -> MED_WAIT -> SET/RES -> SPEED_CONTROL
    -> CANCEL -> MED_WAIT -> CANCEL -> OFF
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from nexo_ai_cruise import NexoAICruiseStateManager, NexoExperimentalModeController

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

  original_init = CarState.__init__
  original_update = CarState.update
  original_update_button_enable = CarState.update_button_enable
  ButtonType = module.ButtonType

  def _new_manager():
    return NexoAICruiseStateManager(
      ButtonType,
      module.BUTTONS_DICT,
      module.create_button_events,
      module.CV.KPH_TO_MS,
      module.CV.MPH_TO_KPH,
    )

  def __init__(self, CP):
    original_init(self, CP)
    if _is_nexo(CP.carFingerprint) and CP.openpilotLongitudinalControl:
      self.main_enabled = False
      self._nexo_ai_cruise = _new_manager()

  def _manager(self):
    mgr = getattr(self, "_nexo_ai_cruise", None)
    if mgr is None:
      mgr = _new_manager()
      self._nexo_ai_cruise = mgr
      self.main_enabled = False
    return mgr

  def update(self, can_parsers):
    ret = original_update(self, can_parsers)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return ret

    mgr = _manager(self)
    stock_main_enabled = bool(self.main_enabled)
    try:
      raw_main = int(self.main_buttons[-1])
    except Exception:
      raw_main = 0
    try:
      raw_button = int(self.cruise_buttons[-1])
    except Exception:
      raw_button = 0

    was_long_enabled = bool(mgr.enabled)
    events = mgr.update(
      ret,
      raw_main,
      raw_button,
      stock_main_enabled,
      bool(self.is_metric),
      ret.buttonEvents,
    )

    filtered = []
    for ev in events:
      if ev.type == ButtonType.cancel and was_long_enabled:
        continue
      filtered.append(ev)

    ret.buttonEvents = filtered
    mgr.apply_to_car_state(ret)
    self.main_enabled = bool(mgr.available)
    return ret

  def update_button_enable(self, button_events):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      mgr = _manager(self)
      if mgr.consume_enable_pulse():
        return True
      if mgr.available:
        return False
    return original_update_button_enable(self, button_events)

  CarState.__init__ = __init__
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


def _patch_selfdrived(module) -> None:
  SelfdriveD = module.SelfdriveD
  if getattr(SelfdriveD, "_nexo_ai_med_patched", False):
    return

  original_update_events = SelfdriveD.update_events
  original_publish_selfdrive_state = getattr(SelfdriveD, "publish_selfdriveState", None)
  original_init = SelfdriveD.__init__

  def __init__(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    cp = getattr(self, "CP", None)
    if cp is not None and _is_nexo(cp.carFingerprint) and cp.openpilotLongitudinalControl:
      self._nexo_experimental_mode = NexoExperimentalModeController()

  def update_events(self, CS):
    original_update_events(self, CS)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return

    controller = getattr(self, "_nexo_experimental_mode", None)
    if controller is None:
      controller = NexoExperimentalModeController()
      self._nexo_experimental_mode = controller
    speed_control = bool(CS.cruiseState.enabled)
    if not speed_control and controller.speed_control_active:
      manual_mode = self.params.get_bool("ExperimentalMode")
    else:
      manual_mode = getattr(self, "experimental_mode", False)
    speed_kph = max(0.0, float(getattr(CS, "vEgo", 0.0)) * 3.6)
    self._nexo_actual_experimental_mode = controller.update(speed_control, speed_kph, manual_mode)
    self.experimental_mode = self._nexo_actual_experimental_mode

    # Brake returns NEXO SPEED_CONTROL to MED. Keep the lateral session alive.
    if CS.cruiseState.available and CS.brakePressed and not CS.gasPressed and not CS.regenBraking:
      pedal_pressed = module.EventName.pedalPressed
      self.events.events = [event for event in self.events.events if event != pedal_pressed]

  def publish_selfdriveState(self, CS):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      actual_mode = getattr(self, "_nexo_actual_experimental_mode", None)
      if actual_mode is not None:
        self.experimental_mode = bool(actual_mode)
    if original_publish_selfdrive_state is not None:
      return original_publish_selfdrive_state(self, CS)

  SelfdriveD.__init__ = __init__
  SelfdriveD.update_events = update_events
  SelfdriveD.publish_selfdriveState = publish_selfdriveState
  SelfdriveD._nexo_ai_med_patched = True


def _patch_card(module) -> None:
  Car = module.Car
  if getattr(Car, "_nexo_ai_med_patched", False):
    return

  original_state_update = Car.state_update

  def state_update(self):
    CS, RD = original_state_update(self)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return CS, RD

    helper = self.v_cruise_helper
    helper._soft_hold_active = 0
    helper._activate_cruise = 0
    helper._paddle_decel_active = False
    helper._cruise_cancel_state = False
    helper._lat_enabled = bool(CS.cruiseState.available)

    target_kph = float(CS.cruiseState.speed) * 3.6 if CS.cruiseState.enabled else 255.0
    helper.v_cruise_kph = target_kph
    helper.v_cruise_cluster_kph = target_kph
    CS.vCruise = target_kph
    CS.vCruiseCluster = target_kph
    CS.softHoldActive = 0
    CS.activateCruise = 0
    CS.latEnabled = bool(CS.cruiseState.available)
    self.CI.CS.softHoldActive = 0
    return CS, RD

  Car.state_update = state_update
  Car._nexo_ai_med_patched = True


def _patch_carcontroller(module) -> None:
  CarController = module.CarController
  if getattr(CarController, "_nexo_ai_med_patched", False):
    return

  original_make_spam_button = CarController.make_spam_button

  def make_spam_button(self, CC, CS):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      return 0
    return original_make_spam_button(self, CC, CS)

  CarController.make_spam_button = make_spam_button
  CarController._nexo_ai_med_patched = True


def _patch_hyundaican(module) -> None:
  if getattr(module, "_nexo_ai_med_patched", False):
    return

  original_scc = module.create_acc_commands_scc
  original_acc = module.create_acc_commands

  def _nexo_set_speed_units(CS, fallback):
    try:
      speed_ms = float(CS.out.cruiseState.speed)
      if speed_ms <= 0.0:
        return fallback
      return speed_ms * (3.6 if bool(CS.is_metric) else 2.2369362920544)
    except Exception:
      return fallback

  def _nexo_med_scc14(packer, jerk, hud_control, CS):
    """Legacy AI MED: SCC12 idle, SCC14 active, no accel request."""
    d = float(getattr(hud_control, "leadDistance", 0.0))
    obj_gap = 0 if d == 0 else 2 if d < 25 else 3 if d < 40 else 4 if d < 70 else 5
    rel_speed = float(getattr(hud_control, "leadRelSpeed", 0.0))
    obj_gap2 = 0 if obj_gap == 0 else 2 if rel_speed < -0.2 else 1
    values = {
      "ComfortBandUpper": jerk.cb_upper,
      "ComfortBandLower": jerk.cb_lower,
      "JerkUpperLimit": jerk.jerk_u,
      "JerkLowerLimit": 0,
      "ACCMode": 1,
      "ObjGap": obj_gap,
      "ObjDistStat": obj_gap2,
    }
    return packer.make_can_msg("SCC14", 0, values)

  def _replace_scc14_for_med(commands, packer, jerk, hud_control, CS):
    med_scc14 = _nexo_med_scc14(packer, jerk, hud_control, CS)
    replaced = []
    found = False
    for cmd in commands:
      try:
        addr = int(cmd[0])
      except Exception:
        addr = -1
      if addr == 0x389:
        replaced.append(med_scc14)
        found = True
      else:
        replaced.append(cmd)
    if not found:
      replaced.append(med_scc14)
    return replaced

  def create_acc_commands_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                              stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode):
    if _is_nexo(CS.CP.carFingerprint):
      available = bool(CS.out.cruiseState.available)
      speed_control = bool(available and CS.out.cruiseState.enabled)
      med_wait = bool(available and not CS.out.cruiseState.enabled)

      if med_wait:
        commands = original_scc(
          packer, False, 0.0, jerk, idx, hud_control, 0,
          False, False, suppress_casper_ev_fca, CS, soft_hold_mode,
        )
        return _replace_scc14_for_med(commands, packer, jerk, hud_control, CS)

      if speed_control:
        set_speed = _nexo_set_speed_units(CS, set_speed)
        return original_scc(
          packer, enabled, accel, jerk, idx, hud_control, set_speed,
          stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode,
        )

      return original_scc(
        packer, False, 0.0, jerk, idx, hud_control, 0,
        False, False, suppress_casper_ev_fca, CS, soft_hold_mode,
      )

    return original_scc(
      packer, enabled, accel, jerk, idx, hud_control, set_speed,
      stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode,
    )

  def create_acc_commands(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                          stopping, long_override, use_fca, CP, CS, soft_hold_mode):
    if _is_nexo(CP.carFingerprint):
      available = bool(CS.out.cruiseState.available)
      speed_control = bool(available and CS.out.cruiseState.enabled)
      med_wait = bool(available and not CS.out.cruiseState.enabled)

      if med_wait:
        commands = original_acc(
          packer, False, 0.0, jerk, idx, hud_control, 0,
          False, False, use_fca, CP, CS, soft_hold_mode,
        )
        return _replace_scc14_for_med(commands, packer, jerk, hud_control, CS)

      enabled = bool(enabled and speed_control)
      if speed_control:
        set_speed = _nexo_set_speed_units(CS, set_speed)
      else:
        accel = 0.0
        stopping = False
        long_override = False
        set_speed = 0

    return original_acc(
      packer, enabled, accel, jerk, idx, hud_control, set_speed,
      stopping, long_override, use_fca, CP, CS, soft_hold_mode,
    )

  module.create_acc_commands_scc = create_acc_commands_scc
  module.create_acc_commands = create_acc_commands
  module._nexo_ai_med_patched = True


_PATCHERS = {
  "opendbc.car.hyundai.carstate": _patch_carstate,
  "opendbc.car.hyundai.carcontroller": _patch_carcontroller,
  "opendbc.car.hyundai.hyundaican": _patch_hyundaican,
  "openpilot.selfdrive.car.card": _patch_card,
  "openpilot.selfdrive.controls.controlsd": _patch_controlsd,
  "openpilot.selfdrive.selfdrived.selfdrived": _patch_selfdrived,
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
