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
      # NEXO must always boot OFF. AutoEngage or a stale CLU11 value must not
      # create CRUISE --- until the driver physically presses MODE.
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

    # Remember whether this edge started while longitudinal control was active.
    # If so, the first CANCEL is consumed so global selfdrive/lateral stays on
    # and the manager alone transitions SPEED_CONTROL -> MED_WAIT.
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

    # Keep the controller-facing CarState.out copy synchronized on the next
    # control cycle through the normal interface path; no synthetic CLU11 input
    # is generated here.
    return ret

  def update_button_enable(self, button_events):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      mgr = _manager(self)
      # MODE produces exactly one buttonEnable pulse for MED/lateral engagement.
      if mgr.consume_enable_pulse():
        return True
      # SET/RES edges remain visible downstream, but they do not need another
      # global enable because MED is already engaged.
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
      # MED_WAIT keeps global/lateral engagement but longitudinal actuation is
      # completely idle until the driver's physical SET/RES release.
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


def _patch_carcontroller(module) -> None:
  CarController = module.CarController
  if getattr(CarController, "_nexo_ai_med_patched", False):
    return

  original_make_spam_button = CarController.make_spam_button

  def make_spam_button(self, CC, CS):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      # AI-style NEXO target speed is changed only by the driver's physical
      # CLU11 buttons. Never inject RES/SET target-chasing frames.
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
    """Make the MED_WAIT SCC split identical to the legacy NEXO AI fork.

    SCC11 MainMode_ACC=1 / VSetDis=0
    SCC12 ACCMode=0 / accel=0
    SCC14 ACCMode=1
    """
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

  @staticmethod
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
        # Call XPlus globally enabled so SCC14 stays ACCMode=1, then replace
        # SCC12 only with the AI idle-longitudinal frame.
        commands = original_scc(
          packer, True, 0.0, jerk, idx, hud_control, 0,
          False, False, suppress_casper_ev_fca, CS, soft_hold_mode,
        )
        return _replace_scc12_with_med(packer, commands, CS, idx)

      if speed_control:
        # The AI manager's target is authoritative for the cluster/SCC as well
        # as carState, preventing the first press from working while later +/-
        # presses modify a different target variable.
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
      enabled = bool(enabled and CS.out.cruiseState.enabled)
      if enabled:
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
