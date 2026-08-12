import numpy as np
from openpilot.cereal import car
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N
from openpilot.common.pid import PIDController
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.common.params import Params

CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]

LongCtrlState = car.CarControl.Actuators.LongControlState


def _is_nexo_1st_gen(CP):
  fingerprint = getattr(CP, "carFingerprint", "")
  name = getattr(fingerprint, "name", str(fingerprint))
  return name == "HYUNDAI_NEXO_1ST_GEN" or str(fingerprint).endswith(".HYUNDAI_NEXO_1ST_GEN")


def _nexo_lead_departing(CP, v_ego, radarState):
  """Return True only when a real radar lead is clearly moving away from a stopped NEXO."""
  if not _is_nexo_1st_gen(CP) or v_ego > 1.0:
    return False

  lead = radarState.leadOne
  if not lead.status:
    return False

  # At a standstill vRel is effectively the lead vehicle speed. Use vLead as a
  # second signal because the radar/model fusion can update the two at slightly
  # different times immediately after the lead starts moving.
  return lead.vRel > 0.35 or lead.vLead > 0.5


def long_control_state_trans(CP, active, long_control_state, v_ego,
                             should_stop, brake_pressed, cruise_standstill, a_ego, stopping_accel, radarState):
  lead_departing = _nexo_lead_departing(CP, v_ego, radarState)

  # The planner can keep shouldStop asserted for a few frames after the lead has
  # already started moving. On first-gen NEXO that kept LongControl latched in
  # stopping indefinitely. Only relax shouldStop when a valid close-loop radar
  # lead is positively moving away; brake and cruise standstill still block start.
  effective_should_stop = should_stop and not lead_departing
  stopping_condition = effective_should_stop
  starting_condition = (not effective_should_stop and
                        not cruise_standstill and
                        not brake_pressed)
  started_condition = v_ego > CP.vEgoStarting

  if not active:
    long_control_state = LongCtrlState.off

  else:
    if long_control_state == LongCtrlState.off:
      if not starting_condition:
        long_control_state = LongCtrlState.stopping
      else:
        if starting_condition and CP.startingState:
          long_control_state = LongCtrlState.starting
        else:
          long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.stopping:
      if starting_condition and CP.startingState:
        long_control_state = LongCtrlState.starting
      elif starting_condition:
        long_control_state = LongCtrlState.pid

    elif long_control_state in [LongCtrlState.starting, LongCtrlState.pid]:
      if stopping_condition:
        stopping_accel = stopping_accel if stopping_accel < 0.0 else -0.5
        leadOne = radarState.leadOne
        # A close lead that is already pulling away must not immediately force
        # NEXO back from starting into stopping just because dRel is still < 4 m.
        fcw_stop = leadOne.status and leadOne.dRel < 4.0 and not lead_departing
        if a_ego > stopping_accel or fcw_stop: # and v_ego < 1.0:
          long_control_state = LongCtrlState.stopping
        if long_control_state == LongCtrlState.starting:
          long_control_state = LongCtrlState.stopping
      elif started_condition:
        long_control_state = LongCtrlState.pid
  return long_control_state

class LongControl:
  def __init__(self, CP):
    self.CP = CP
    self.long_control_state = LongCtrlState.off
    self.pid = PIDController((CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV),
                             (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV),
                             k_f=CP.longitudinalTuning.kf, rate=1 / DT_CTRL)
    self.last_output_accel = 0.0


    self.params = Params()
    self.readParamCount = 0
    self.stopping_accel = 0
    self.j_lead = 0.0

    self.use_accel_pid = False
    if CP.brand == "toyota":
      self.use_accel_pid = True

  def reset(self):
    self.pid.reset()

  def update(self, active, CS, long_plan, accel_limits, t_since_plan, radarState):

    soft_hold_active = CS.softHoldActive > 0
    a_target_ff = long_plan.aTarget
    v_target_now = long_plan.vTargetNow
    j_target_now = long_plan.jTargetNow
    should_stop = long_plan.shouldStop

    self.readParamCount += 1
    if self.readParamCount >= 100:
      self.readParamCount = 0
      self.stopping_accel = self.params.get_float("StoppingAccel") * 0.01
    elif self.readParamCount == 10:
      if len(self.CP.longitudinalTuning.kpBP) == 1 and len(self.CP.longitudinalTuning.kiBP)==1:
        longitudinalTuningKpV = self.params.get_float("LongTuningKpV") * 0.01
        longitudinalTuningKiV = self.params.get_float("LongTuningKiV") * 0.001
        self.pid._k_p = (self.CP.longitudinalTuning.kpBP, [longitudinalTuningKpV])
        self.pid._k_i = (self.CP.longitudinalTuning.kiBP, [longitudinalTuningKiV])
        self.pid.k_f = self.params.get_float("LongTuningKf") * 0.01


    """Update longitudinal control. This updates the state machine and runs a PID loop"""
    self.pid.neg_limit = accel_limits[0]
    self.pid.pos_limit = accel_limits[1]

    self.long_control_state = long_control_state_trans(self.CP, active, self.long_control_state, CS.vEgo,
                                                       should_stop, CS.brakePressed,
                                                       CS.cruiseState.standstill, CS.aEgo, self.stopping_accel, radarState)
    if active and soft_hold_active:
      self.long_control_state = LongCtrlState.stopping

    if self.long_control_state == LongCtrlState.off:
      self.reset()
      output_accel = 0.

    elif self.long_control_state == LongCtrlState.stopping:
      output_accel = self.last_output_accel

      if soft_hold_active:
        output_accel = self.CP.stopAccel

      stopAccel = self.stopping_accel if self.stopping_accel < 0.0 else self.CP.stopAccel
      if output_accel > stopAccel:
        output_accel = min(output_accel, 0.0)
        output_accel -= self.CP.stoppingDecelRate * DT_CTRL
      self.reset()

    elif self.long_control_state == LongCtrlState.starting:
      output_accel = self.CP.startAccel
      self.reset()

    else:  # LongCtrlState.pid
      if self.use_accel_pid:
        error = a_target_ff - CS.aEgo
      else:
        error = v_target_now - CS.vEgo
      output_accel = self.pid.update(error, speed=CS.vEgo,
                                     feedforward=a_target_ff)

    self.last_output_accel = np.clip(output_accel, accel_limits[0], accel_limits[1])
    return self.last_output_accel, a_target_ff, j_target_now
