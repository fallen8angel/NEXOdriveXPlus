"""Exercise the inline XPlus detector without importing native driving processes."""

import ast
import math
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.common.constants import CV


DT = 0.05
SOURCE = Path(__file__).resolve().parents[1] / "carrot_functions.py"


def _load_driving_mode():
  # Compile the production definitions, as the cluster tests do for native
  # process methods. No replacement detector or global module stubs are used.
  tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
  definitions = []
  for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name in ("DrivingMode", "DrivingModeDetector", "CarrotPlanner"):
      if node.name == "CarrotPlanner":
        node.body = [method for method in node.body if isinstance(method, ast.FunctionDef)
                     and method.name in ("_params_update", "_update_driving_mode")]
      definitions.append(node)
  namespace = {"Enum": Enum, "math": math, "CV": CV, "DT_MDL": DT}
  exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE), "exec"), namespace)
  return (namespace[name] for name in ("DrivingMode", "DrivingModeDetector", "CarrotPlanner"))


DrivingMode, DrivingModeDetector, CarrotPlanner = _load_driving_mode()


def car(kph=10):
  return SimpleNamespace(vEgo=kph * CV.KPH_TO_MS)


def lead(kph=0, **overrides):
  values = dict(status=True, dRel=10.0, vLead=kph * CV.KPH_TO_MS,
                vRel=0.0, aLeadK=0.0, radar=True, radarTrackId=7)
  values.update(overrides)
  return SimpleNamespace(**values)


def advance(detector, seconds, carstate=None, lead_one=None, **kwargs):
  for _ in range(round(seconds / DT)):
    detector.update_data(carstate or car(), lead_one or lead(), **kwargs)


def safe_detector():
  detector = DrivingModeDetector()
  advance(detector, 0.3)
  assert detector.get_mode() == DrivingMode.Safe
  return detector


def test_stopping_requires_continuous_point_three_seconds():
  detector = DrivingModeDetector()
  advance(detector, 0.25)
  assert detector.get_mode() == DrivingMode.Normal
  detector.update_data(car(), lead(20))
  advance(detector, 0.25)
  assert detector.get_mode() == DrivingMode.Normal
  detector.update_data(car(), lead())
  assert detector.get_mode() == DrivingMode.Safe


def test_stopped_lead_enters_safe_during_higher_speed_approach():
  detector = DrivingModeDetector()
  advance(detector, 0.3, car(60), lead(dRel=80))
  assert detector.get_mode() == DrivingMode.Safe


def test_sustained_slow_following_requires_eight_seconds():
  detector = DrivingModeDetector()
  advance(detector, 7.95, car(25), lead(20, dRel=25))
  assert detector.get_mode() == DrivingMode.Normal
  advance(detector, 0.1, car(25), lead(20, dRel=25))
  assert detector.get_mode() == DrivingMode.Safe


@pytest.mark.parametrize("accel", [0.0, 1.0])
def test_acceleration_exit_threshold_is_strict(accel):
  detector = safe_detector()
  advance(detector, 1, car(10), lead(10, aLeadK=accel))
  assert detector.get_mode() == DrivingMode.Safe


def test_sustained_acceleration_exits_after_half_second():
  detector = safe_detector()
  accelerating = lead(10, aLeadK=1.01)
  advance(detector, 0.45, car(10), accelerating)
  assert detector.get_mode() == DrivingMode.Safe
  advance(detector, 0.1, car(10), accelerating)
  assert detector.get_mode() == DrivingMode.Normal
  assert detector.get_mode(2) == DrivingMode.Eco


def test_acceleration_spike_does_not_release_safe():
  detector = safe_detector()
  advance(detector, 0.4, car(10), lead(10, aLeadK=2))
  advance(detector, 0.1, car(10), lead(10))
  advance(detector, 0.4, car(10), lead(10, aLeadK=2))
  assert detector.get_mode() == DrivingMode.Safe


@pytest.mark.parametrize("car_kph,lead_kph,distance,v_rel", [(40, 40, 30, 0), (20, 25, 40, 1)])
def test_flow_recovery_requires_three_seconds(car_kph, lead_kph, distance, v_rel):
  detector = safe_detector()
  moving = lead(lead_kph, dRel=distance, vRel=v_rel)
  advance(detector, 2.95, car(car_kph), moving)
  assert detector.get_mode() == DrivingMode.Safe
  advance(detector, 0.1, car(car_kph), moving)
  assert detector.get_mode() == DrivingMode.Normal


def test_decelerating_lead_resets_flow_recovery():
  detector = safe_detector()
  advance(detector, 2.9, car(40), lead(40))
  advance(detector, 0.1, car(40), lead(40, aLeadK=-0.3))
  advance(detector, 2.9, car(40), lead(40))
  assert detector.get_mode() == DrivingMode.Safe


@pytest.mark.parametrize("kph", [0, 5, 14.9])
def test_lead_disappearing_at_low_speed_does_not_release_safe(kph):
  detector = safe_detector()
  advance(detector, 10, car(kph), lead(status=False))
  assert detector.get_mode() == DrivingMode.Safe


def test_clear_road_requires_four_seconds_at_driving_speed():
  detector = safe_detector()
  advance(detector, 3.95, car(20), lead(status=False))
  assert detector.get_mode() == DrivingMode.Safe
  advance(detector, 0.1, car(20), lead(status=False))
  assert detector.get_mode() == DrivingMode.Normal


@pytest.mark.parametrize("changed", [dict(radarTrackId=8), dict(radar=False)])
def test_new_lead_does_not_inherit_acceleration_or_recovery(changed):
  detector = safe_detector()
  advance(detector, 0.4, car(40), lead(40, aLeadK=2))
  advance(detector, 0.2, car(40), lead(40, aLeadK=2, **changed))
  assert detector.get_mode() == DrivingMode.Safe
  detector = safe_detector()
  advance(detector, 2.9, car(40), lead(40))
  advance(detector, 0.2, car(40), lead(40, **changed))
  assert detector.get_mode() == DrivingMode.Safe


@pytest.mark.parametrize("field", ["dRel", "vLead", "vRel", "aLeadK"])
@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_nonfinite_lead_values_reset_evidence_without_releasing_safe(field, invalid):
  detector = safe_detector()
  advance(detector, 0.4, car(10), lead(10, aLeadK=2))
  bad = lead(10, aLeadK=2)
  setattr(bad, field, invalid)
  advance(detector, 1, car(10), bad)
  advance(detector, 0.2, car(10), lead(10, aLeadK=2))
  assert detector.get_mode() == DrivingMode.Safe


@pytest.mark.parametrize("distance", [0, -1])
def test_invalid_distance_cannot_release_safe(distance):
  detector = safe_detector()
  advance(detector, 1, car(40), lead(40, dRel=distance, aLeadK=2))
  assert detector.get_mode() == DrivingMode.Safe


@pytest.mark.parametrize("dt", [0, -0.05, 0.21, math.nan, math.inf])
def test_invalid_timing_does_not_release_safe(dt):
  detector = safe_detector()
  advance(detector, 0.4, car(10), lead(10, aLeadK=2))
  detector.update_data(car(10), lead(10, aLeadK=2), dt=dt)
  advance(detector, 0.2, car(10), lead(10, aLeadK=2))
  assert detector.get_mode() == DrivingMode.Safe


def test_invalid_ego_speed_and_stale_data_do_not_release_safe():
  detector = safe_detector()
  advance(detector, 1, car(math.nan), lead(40, aLeadK=2))
  advance(detector, 5, car(40), lead(status=False), valid=False)
  assert detector.get_mode() == DrivingMode.Safe


def test_stopping_has_priority_over_acceleration_exit():
  detector = safe_detector()
  advance(detector, 4, car(10), lead(4, aLeadK=3))
  assert detector.get_mode() == DrivingMode.Safe
  assert detector.get_mode(2) == DrivingMode.Safe


class SubMasterStub(dict):
  def __init__(self, carstate, lead_one):
    super().__init__(carState=carstate, radarState=SimpleNamespace(leadOne=lead_one))
    self.logMonoTime = {"radarState": 1_000_000_000}
    self.valid = True

  def all_checks(self, services):
    assert services == ["carState", "radarState"]
    return self.valid


def planner(auto=1, mode=3):
  result = CarrotPlanner()
  result.drivingModeDetector = safe_detector()
  result._driving_mode_radar_time = None
  result.myDrivingModeAuto = auto
  result.myDrivingMode_disable_auto = False
  result.myDrivingMode = result.myDrivingMode_last = DrivingMode(mode)
  result.frame = 0
  result.params_count = 49
  values = {"MyDrivingMode": mode, "MyDrivingModeAuto": auto}
  result.params = SimpleNamespace(get_int=lambda key: values[key])
  return result, values


def test_repeated_radar_sample_cannot_accumulate_acceleration_or_clear_road():
  for lead_one in (lead(40, aLeadK=2), lead(status=False)):
    value, _ = planner()
    sm = SubMasterStub(car(40), lead_one)
    for _ in range(200):
      value._update_driving_mode(sm)
    assert value.myDrivingMode == DrivingMode.Safe


def test_stale_submaster_and_timestamp_gap_reset_recovery():
  value, _ = planner()
  sm = SubMasterStub(car(40), lead(40, aLeadK=2))
  for _ in range(8):
    value._update_driving_mode(sm)
    sm.logMonoTime["radarState"] += 50_000_000
  sm.valid = False
  value._update_driving_mode(sm)
  sm.valid = True
  value._update_driving_mode(sm)
  sm.logMonoTime["radarState"] += 1_000_000_000
  value._update_driving_mode(sm)
  assert value.myDrivingMode == DrivingMode.Safe
  assert value.drivingModeDetector.accel_time == 0


def test_invalid_ego_between_radar_samples_discards_previous_acceleration():
  value, _ = planner()
  sm = SubMasterStub(car(40), lead(40, aLeadK=2))
  for _ in range(8):
    sm.logMonoTime["radarState"] += 50_000_000
    value._update_driving_mode(sm)
  # The carState input can change even when radarState is still cached.
  sm["carState"] = car(math.nan)
  value._update_driving_mode(sm)
  assert value.drivingModeDetector.accel_time == 0
  sm["carState"] = car(40)
  for _ in range(4):
    sm.logMonoTime["radarState"] += 50_000_000
    value._update_driving_mode(sm)
  assert value.myDrivingMode == DrivingMode.Safe


def test_fresh_radar_updates_apply_mode_without_parameter_poll_delay():
  value, _ = planner()
  value.drivingModeDetector = DrivingModeDetector()
  sm = SubMasterStub(car(), lead())
  for _ in range(6):
    value._update_driving_mode(sm)
    sm.logMonoTime["radarState"] += 50_000_000
  assert value.myDrivingMode == DrivingMode.Safe


@pytest.mark.parametrize("manual_mode", [1, 2, 4])
def test_manual_mode_change_remains_authoritative(manual_mode):
  value, values = planner()
  values["MyDrivingMode"] = manual_mode
  value._params_update()
  assert value.myDrivingMode_disable_auto
  sm = SubMasterStub(car(40), lead(40, aLeadK=2))
  for _ in range(80):
    value._update_driving_mode(sm)
    sm.logMonoTime["radarState"] += 50_000_000
  assert value.myDrivingMode == DrivingMode(manual_mode)
  assert values == {"MyDrivingMode": manual_mode, "MyDrivingModeAuto": 1}


@pytest.mark.parametrize("manual_mode", [1, 2, 3, 4])
def test_auto_disabled_preserves_all_manual_modes(manual_mode):
  value, _ = planner(auto=0, mode=manual_mode)
  value._params_update()
  value._update_driving_mode(SubMasterStub(car(), lead()))
  assert value.myDrivingMode == DrivingMode(manual_mode)


def test_saved_auto_eco_mode_recovers_to_eco():
  value, _ = planner(auto=2)
  value.drivingModeDetector = DrivingModeDetector()
  value._params_update()
  value._update_driving_mode(SubMasterStub(car(40), lead(40)))
  assert value.myDrivingMode == DrivingMode.Eco
