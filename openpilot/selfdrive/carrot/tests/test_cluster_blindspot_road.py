from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cluster"))
from cluster_models import ClusterUiState, LaneMarking, ModelPathPoint
from cluster_scene import build_cluster_scene, cluster_scene_state_key


def state(**changes):
  base = ClusterUiState(
    speed_kph=0, accel_mps2=0, steering=0, speed_limit_kph=None,
    speed_limit_source=None, cruise_kph=None, cruise_display_state="off",
    gear_text=None, cruise_gap=None, lfa_active=None,
    left_signal=False, right_signal=False, left_blindspot=False, right_blindspot=False,
    lane_change=None, lane_change_phase="idle", lane_change_progress=0,
    highlight_lane=None, highlight_lane_offset=None, ego_lane_offset=0,
    road_view_lane_position=0, camera_lane_center_offset_m=None, lane_width_m=3.6,
    steering_angle_deg=0, surround_yaw_deg=0, surround_pitch_deg=0,
    surround_view_active=False, lanes=(),
  )
  return replace(base, **changes)


def warnings(value, **kwargs):
  return [strip for strip in build_cluster_scene(value, **kwargs).highlight_lanes if strip.color == (255, 0, 0, 190)]


class TestClusterBlindspotRoad(unittest.TestCase):
  def test_each_side_both_and_release_without_blinkers(self):
    for left, right, expected in ((True, False, [-1]), (False, True, [1]),
                                  (True, True, [-1, 1]), (False, False, [])):
      with self.subTest(left=left, right=right):
        strips = warnings(state(left_blindspot=left, right_blindspot=right), highlight_lane_lit=False)
        self.assertEqual(len(strips), len(expected))
        for strip, side in zip(strips, expected):
          self.assertTrue(all(point.x * side > 0 for point in strip.left + strip.right))
          self.assertTrue(all(point.z == 0.008 for point in strip.left + strip.right))
          for a, b in zip(strip.left, strip.right):
            self.assertAlmostEqual(b.x - a.x, 2.79)

  def test_follows_curved_ego_boundaries_without_outer_lanes(self):
    for side in (-1, 1):
      points = tuple(ModelPathPoint(x, side * 1.8 + x * 0.01) for x in (0, 10, 20, 40, 80))
      value = state(left_blindspot=side == -1, right_blindspot=side == 1,
                    lanes=(LaneMarking(side * 0.5, model_points=points),))
      strip, = warnings(value)
      self.assertGreater(strip.left[-1].x, strip.left[0].x)
      for a, b in zip(strip.left, strip.right):
        self.assertAlmostEqual(b.x - a.x, 2.79)

  def test_missing_model_on_one_side_does_not_hide_other_side(self):
    points = (ModelPathPoint(0, -1.8), ModelPathPoint(80, -1.8))
    value = state(left_blindspot=True, right_blindspot=True,
                  lanes=(LaneMarking(-0.5, model_points=points),))
    self.assertEqual(len(warnings(value)), 2)

  def test_camera_modes_and_surround_share_warning_mesh(self):
    for camera_mode in (0, 1, 2, 3):
      for surround in (False, True):
        with self.subTest(camera_mode=camera_mode, surround=surround):
          self.assertEqual(len(warnings(state(right_blindspot=True, camera_view_mode=camera_mode,
                                              surround_view_active=surround))), 1)

  def test_blinkers_alone_do_not_create_red_warning(self):
    self.assertEqual(warnings(state(left_signal=True, right_signal=True,
                                    highlight_lane="left", highlight_lane_offset=-1)), [])

  def test_release_invalidates_scene_cache_and_removes_warning(self):
    active = state(right_blindspot=True)
    cleared = replace(active, right_blindspot=False)
    self.assertNotEqual(cluster_scene_state_key(active), cluster_scene_state_key(cleared))
    self.assertEqual(len(warnings(active)), 1)
    self.assertEqual(warnings(cleared), [])


if __name__ == "__main__":
  unittest.main()
