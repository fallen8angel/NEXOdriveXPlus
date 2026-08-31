from types import SimpleNamespace

import pytest

from openpilot.selfdrive.ui.onroad.vehicle_navi import onroad_speed_source_label, vehicle_navi_camera_active


@pytest.mark.parametrize(
  ("speed_limit", "expected"),
  ((0, False), (30, True), (60, True), (float("nan"), False)),
)
def test_vehicle_navi_indicator_follows_stock_camera_signal(speed_limit, expected):
  sm = {"carState": SimpleNamespace(speedLimit=speed_limit)}
  assert vehicle_navi_camera_active(sm) is expected


def test_vehicle_navi_indicator_handles_missing_car_state():
  assert not vehicle_navi_camera_active({})


@pytest.mark.parametrize(
  ("source", "expected"),
  (("hda", "vNAVI"), ("HDA", "vNAVI"), ("cam", "cam"), ("", "apply")),
)
def test_stock_navigation_deceleration_uses_vehicle_navi_label(source, expected):
  assert onroad_speed_source_label(source) == expected
