import ast
import json
from pathlib import Path

import numpy as np
import pytest

from openpilot.common.stopping_params import get_stopping_speed, normalize_stopping_speed


class FakeParams:
  def __init__(self, value):
    self.value = value
    self.writes = []

  def get(self, key):
    assert key == "VEgoStopping"
    return self.value

  def put_int(self, key, value):
    assert key == "VEgoStopping"
    assert isinstance(value, int)
    self.value = value
    self.writes.append((key, value))


@pytest.mark.parametrize("stored", [-10, -1, 0, *range(1, 10)])
def test_legacy_value_is_safe_before_and_after_boot_normalization(stored):
  params = FakeParams(stored)
  assert get_stopping_speed(params) == pytest.approx(0.10)
  assert params.writes == []

  normalize_stopping_speed(params)
  assert params.value == 10
  assert params.writes == [("VEgoStopping", 10)]
  assert get_stopping_speed(params) == pytest.approx(0.10)

  normalize_stopping_speed(params)
  assert params.writes == [("VEgoStopping", 10)]


@pytest.mark.parametrize("stored", [10, 15, 20, 50, 100])
def test_valid_setting_and_default_are_preserved(stored):
  params = FakeParams(stored)
  normalize_stopping_speed(params)
  assert params.value == stored
  assert params.writes == []
  assert get_stopping_speed(params) == pytest.approx(stored * 0.01)


@pytest.mark.parametrize("stored", [None, "", "invalid", "nan", float("nan"), float("inf"), float("-inf")])
def test_invalid_setting_uses_existing_default(stored):
  params = FakeParams(stored)
  assert get_stopping_speed(params) == pytest.approx(0.50)
  assert params.writes == []

  normalize_stopping_speed(params)
  assert params.value == 50
  assert params.writes == [("VEgoStopping", 50)]


def test_live_setting_changes_always_enforce_floor_without_writes():
  params = FakeParams(50)
  assert get_stopping_speed(params) == pytest.approx(0.50)
  for value in range(1, 10):
    params.value = value
    assert get_stopping_speed(params) == pytest.approx(0.10)
  params.value = 20
  assert get_stopping_speed(params) == pytest.approx(0.20)
  assert params.writes == []


def test_settings_expose_safe_minimum_and_keep_default():
  settings_path = Path(__file__).parents[2] / "selfdrive" / "carrot_settings.json"
  settings = json.loads(settings_path.read_text(encoding="utf-8"))
  setting = next(item for item in settings["params"] if item["name"] == "VEgoStopping")
  assert setting["min"] == 10
  assert setting["default"] == 50
  assert setting["max"] == 100
  assert setting["unit"] == 5


@pytest.fixture(scope="module")
def get_accel_from_plan():
  # Exercise the production plan calculation without loading native vehicle
  # dependencies. This is a function-level test, not a planner process replay.
  source_path = Path(__file__).parents[2] / "selfdrive" / "controls" / "lib" / "drive_helpers.py"
  tree = ast.parse(source_path.read_text(encoding="utf-8"))
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_accel_from_plan")
  namespace = {"np": np, "DT_MDL": 0.05}
  exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
  return namespace["get_accel_from_plan"]


def test_floor_recognizes_slow_decelerating_plan(get_accel_from_plan):
  times = np.array([0.0, 0.3, 1.3])
  speeds = np.array([0.1, 0.066716544, 0.030907153])
  accels = np.array([-0.13, -0.08, 0.0])
  assert not get_accel_from_plan(speeds, accels, times, action_t=0.3, vEgoStopping=0.02)[1]
  threshold = get_stopping_speed(FakeParams(2))
  assert get_accel_from_plan(speeds, accels, times, action_t=0.3, vEgoStopping=threshold)[1]


def test_floor_keeps_accelerating_plan_moving(get_accel_from_plan):
  times = np.array([0.0, 0.3, 1.3])
  speeds = np.array([0.1, 0.066716544, 0.2])
  accels = np.array([-0.13, -0.08, 0.1])
  threshold = get_stopping_speed(FakeParams(2))
  assert not get_accel_from_plan(speeds, accels, times, action_t=0.3, vEgoStopping=threshold)[1]
