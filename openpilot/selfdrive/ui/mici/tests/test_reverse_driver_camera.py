import pytest

from openpilot.selfdrive.ui.mici.layouts.main import MiciMainLayout


@pytest.mark.parametrize(
  "enabled,started,reverse_selected,expected",
  [
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, False),
    (True, True, True, True),
  ],
)
def test_reverse_driver_camera_activation_conditions(enabled, started, reverse_selected, expected):
  assert MiciMainLayout._should_show_reverse_camera(enabled, started, reverse_selected) is expected
