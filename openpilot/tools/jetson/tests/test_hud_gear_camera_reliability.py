"""Read-only NEXO HUD gear fallback and camera preview age guards."""
import pytest

from openpilot.tools.jetson.snapshot import verified_hud_gear
from openpilot.tools.jetson.camera import retain_recent_previews


@pytest.mark.parametrize('gear,ready,raw,expected', [
  ('park', True, None, 'park'),
  ('reverse', True, None, 'reverse'),
  ('drive', True, None, 'drive'),
  ('neutral', True, None, 'neutral'),
  ('reverse', True, 'R', 'reverse'),
  ('park', True, 'P', 'park'),
  ('park', True, 'R', 'unknown'),
  ('park', True, 'invalid', 'unknown'),
  ('reverse', True, 'P', 'unknown'),
  ('park', False, None, 'unknown'),
  ('reverse', False, 'R', 'unknown'),
  ('unknown', True, None, 'unknown'),
])
def test_fresh_carstate_works_when_raw_gear_subscription_misses(gear, ready, raw, expected):
  assert verified_hud_gear(gear, ready, raw) == expected


def test_camera_frame_retained_across_one_empty_sample_without_renewing_timestamp():
  frame = {'time': 100.0, 'jpeg': 'original'}
  result = retain_recent_previews({}, {'driver': frame}, {'driver': object()}, 100.2)
  assert result['driver'] is frame
  assert result['driver']['time'] == 100.0


def test_camera_frame_expires_even_if_worker_was_active():
  frame = {'time': 100.0, 'jpeg': 'original'}
  assert retain_recent_previews({}, {'driver': frame}, {'driver': object()}, 100.301) == {}


def test_driver_preview_clears_immediately_outside_r_or_side_view():
  frame = {'time': 100.0, 'jpeg': 'original'}
  assert retain_recent_previews({}, {'driver': frame}, {'road': object()}, 100.01) == {}


def test_new_camera_sample_wins_and_stream_errors_cannot_retain_old_frames():
  new = {'time': 100.2}
  old = {'time': 100.0}
  assert retain_recent_previews({'driver': new}, {'driver': old}, {'driver': object()}, 100.22)['driver'] is new
  assert retain_recent_previews({}, {'driver': old}, {}, 100.1) == {}
