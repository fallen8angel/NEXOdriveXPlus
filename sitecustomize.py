"""NEXO 1st-gen AI/MED compatibility integration.

NEXO uses one dedicated state manager for the complete legacy-AI flow:
  OFF -> MODE -> MED_WAIT -> SET/RES -> SPEED_CONTROL
      -> CANCEL -> MED_WAIT -> CANCEL -> OFF

The manager owns available/enabled/target speed and raw CLU11 button state.
XPlus synthetic speed-button chasing is disabled for NEXO so physical +/-
presses remain authoritative and cannot be locked out by injected CLU11 bursts.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from nexo_ai_cruise import NexoAICruiseStateManager

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

    events = mgr.update(
      ret,
      raw_main,
      raw_button,
      stock_main_enabled,
      bool(self.is_metric),
      ret.buttonEvents,
    )

    # Physical CANCEL is intentionally passed downstream so it exits MED and
    # disengages lateral. Brake never creates a cancel event here; it only
    # changes the manager from SPEED_CONTROL to MED_WAIT.
    ret.buttonEvents = events
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

  def update_events(self, CS):
    original_update_events(self, CS)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return

    # NEXOdriveAI keeps lateral/MED engaged on brake and lets the cruise state
    # manager cancel only the longitudinal target. Preserve accelerator and
    # regen disengagement behavior, and do not mask brake outside MED.
    if CS.cruiseState.available and CS.brakePressed and not CS.gasPressed and not CS.regenBraking:
      pedal_pressed = module.EventName.pedalPressed
      self.events.events = [event for event in self.events.events if event != pedal_pressed]

  SelfdriveD.update_events = update_events
  SelfdriveD._nexo_ai_med_patched = True


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

  def _replace_scc12_with_med(packer, commands, CS, idx):
    if CS.scc12 is None:
      return commands
    try:
      values = dict(CS.scc12)
      values["ACCMode"] = 0
      values["StopReq"] = 0
      values["aReqRaw"] = 0.0
      values["aReqValue"] = 0.0
      if "ACCFailInfo" in values:
        values["ACCFailInfo"] = 0
      values["CR_VSM_ChkSum"] = 0
      values["CR_VSM_Alive"] = idx % 0xF
      dat0 = packer.make_can_msg("SCC12", 0, values)[1]
      values["CR_VSM_ChkSum"] = 0x10 - sum(sum(divmod(i, 16)) for i in dat0) % 0x10
      replacement = packer.make_can_msg("SCC12", 0, values)

      out = []
      replaced = False
      for msg in commands:
        try:
          if int(msg[0]) == 0x421:
            out.append(replacement)
            replaced = True
          else:
            out.append(msg)
        except Exception:
          out.append(msg)
      if not replaced:
        out.append(replacement)
      return out
    except Exception:
      return commands

  def _nexo_set_speed_units(CS, fallback):
    try:
      speed_ms = float(CS.out.cruiseState.speed)
      if speed_ms <= 0.0:
        return fallback
      return speed_ms * (3.6 if bool(CS.is_metric) else 2.2369362920544)
    except Exception:
      return fallback

  def create_acc_commands_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                              stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode):
    if _is_nexo(CS.CP.carFingerprint):
      available = bool(CS.out.cruiseState.available)
      speed_control = bool(available and CS.out.cruiseState.enabled)
      med_wait = bool(available and not CS.out.cruiseState.enabled)

      if med_wait:
        # AI MED split: SCC11 main ON / VSet 0, SCC12 idle, SCC14 active.
        commands = original_scc(
          packer, True, 0.0, jerk, idx, hud_control, 0,
          False, False, suppress_casper_ev_fca, CS, soft_hold_mode,
        )
        return _replace_scc12_with_med(packer, commands, CS, idx)

      if speed_control:
        # Manager target speed is authoritative for every repeated physical +/-.
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
        # The first-gen NEXO is a bus-0 SCC car, so it uses this function rather
        # than create_acc_commands_scc().  Legacy NEXOdriveAI deliberately keeps
        # SCC14 active while SCC12 remains idle in MED_WAIT:
        #   SCC11 MainMode_ACC=1/VSetDis=0, SCC12 ACCMode=0, SCC14 ACCMode=1.
        commands = original_acc(
          packer, True, 0.0, jerk, idx, hud_control, 0,
          False, False, use_fca, CP, CS, soft_hold_mode,
        )
        return _replace_scc12_with_med(packer, commands, CS, idx)

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
