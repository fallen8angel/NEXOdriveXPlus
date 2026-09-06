import pytest

from openpilot.selfdrive.ui.mici.reverse_camera_state import reverse_camera_action, should_show_reverse_camera


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
  assert should_show_reverse_camera(enabled, started, reverse_selected) is expected


@pytest.mark.parametrize(
  "requested,has_dialog,in_stack,closing,expected",
  [
    (True, False, False, False, "create"),
    (True, True, True, False, "wait"),
    (False, True, True, False, "dismiss"),
    (False, True, False, False, "close"),
    (True, True, False, True, "wait"),
    (False, True, False, True, "wait"),
  ],
)
def test_reverse_driver_camera_transition_prevents_duplicates(requested, has_dialog, in_stack, closing, expected):
  assert reverse_camera_action(requested, has_dialog, in_stack, closing) == expected
