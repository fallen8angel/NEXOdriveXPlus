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
    ret = original_update(self, can_parsers)
    if not _is_nexo(self.CP.carFingerprint) or not self.CP.openpilotLongitudinalControl:
      return ret

    stock_main_enabled = bool(self.main_enabled)
    _init_state(self)

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

    if self._nexo_ai_main_armed and stock_main_enabled != self._nexo_ai_stock_main_prev:
      was_available = self._nexo_ai_available
      self._nexo_ai_available = stock_main_enabled
      if self._nexo_ai_available and not was_available:
        self._nexo_ai_med_enable_pulse = True
      if not self._nexo_ai_available:
        self._nexo_ai_long_enabled = False
    self._nexo_ai_stock_main_prev = stock_main_enabled

    raw_events = _raw_cruise_events(self)
    input_events = _merge_events(ret.buttonEvents, raw_events)

    filtered_events = []
    for ev in input_events:
      typ = ev.type

      if typ == ButtonType.mainCruise:
        if self._nexo_ai_main_armed:
          filtered_events.append(ev)
        continue

      if typ in (ButtonType.accelCruise, ButtonType.decelCruise):
        if not ev.pressed and self._nexo_ai_available:
          self._nexo_ai_long_enabled = True
        filtered_events.append(ev)
        continue

      if typ == ButtonType.cancel:
        if ev.pressed and self._nexo_ai_long_enabled:
          self._nexo_ai_long_enabled = False
          self._nexo_ai_cancel_to_med = True
          continue

        if not ev.pressed and self._nexo_ai_cancel_to_med:
          self._nexo_ai_cancel_to_med = False
          continue

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
    if not self._nexo_ai_long_enabled:
      ret.cruiseState.speed = 0.0

    return ret

  def update_button_enable(self, button_events):
    if _is_nexo(self.CP.carFingerprint) and self.CP.openpilotLongitudinalControl:
      _init_state(self)
      if self._nexo_ai_med_enable_pulse:
        self._nexo_ai_med_enable_pulse = False
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

  def _replace_scc12_with_med(packer, commands, CS, idx):
    """AI MED state: SCC12 ACCMode=0 while SCC14 stays ACCMode=1.

    We call the XPlus generator as globally enabled so its SCC14 remains active,
    then replace only SCC12 with the AI idle-longitudinal state. This preserves
    XPlus counters/options while matching the proven NEXOdriveAI MED split.
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

  def create_acc_commands_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                              stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode):
    if _is_nexo(CS.CP.carFingerprint):
      med_wait = bool(CS.out.cruiseState.available and not CS.out.cruiseState.enabled)
      if med_wait:
        # Match NEXOdriveAI exactly:
        #   SCC11 MainMode_ACC=1, VSetDis=0
        #   SCC12 ACCMode=0, accel=0
        #   SCC14 ACCMode=1 (global/lateral MED remains active)
        commands = original_scc(packer, True, 0.0, jerk, idx, hud_control, 0,
                                False, False, suppress_casper_ev_fca, CS, soft_hold_mode)
        return _replace_scc12_with_med(packer, commands, CS, idx)

      # SPEED_CONTROL/OFF: use normal XPlus behavior. Longitudinal activation is
      # already gated by CarState.cruiseState.enabled and controlsd longActive.
      if CS.out.cruiseState.available and CS.out.cruiseState.enabled:
        return original_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                            stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode)

      return original_scc(packer, False, 0.0, jerk, idx, hud_control, 0,
                          False, False, suppress_casper_ev_fca, CS, soft_hold_mode)

    return original_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                        stopping, long_override, suppress_casper_ev_fca, CS, soft_hold_mode)

  def create_acc_commands(packer, enabled, accel, jerk, idx, hud_control, set_speed,
                          stopping, long_override, use_fca, CP, CS, soft_hold_mode):
    if _is_nexo(CP.carFingerprint):
      # Non-SCC copy path: retain the previous safe longitudinal gate.
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
