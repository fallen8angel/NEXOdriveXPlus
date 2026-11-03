"""Display-only false-green suppression; requires no live camera, car or Jetson."""
import json

import cv2
import numpy as np

from openpilot.selfdrive.modeld.hud_signal_filter import filter_for_hud, green_lamp_verified
from openpilot.selfdrive.carrot.signal_assist_runtime import publish_observation
from openpilot.tools.jetson import hud_observations


BOX = [100, 100, 150, 120]


def frame(rgb=(0, 0, 0)):
  return np.full((760, 1344, 3), rgb, dtype=np.uint8)


def sample_result(raw='green'):
  return {'state': raw, 'reason': 'test',
          'tracks': [{'id': 1, 'box': BOX, 'state': raw, 'age': 0.,
                      'observations': 3, 'seen_red': True,
                      'evidence': {'raw': raw, 'source': 'test'}}]}


def green_core_image():
  rgb = frame()
  cv2.circle(rgb, (143, 110), 4, (0, 250, 20), -1)
  return rgb


def test_lit_green_core_is_verified():
  assert green_lamp_verified(green_core_image(), BOX)


def test_blue_cyan_sky_is_not_green_lamp():
  assert not green_lamp_verified(frame((91, 131, 138)), BOX)


def test_flat_green_background_does_not_have_housing_contrast():
  assert not green_lamp_verified(frame((0, 140, 0)), BOX)


def test_no_light_and_invalid_boxes_fail_closed():
  assert not green_lamp_verified(frame(), BOX)
  assert not green_lamp_verified(green_core_image(), [-1, 100, 150, 120])
  assert not green_lamp_verified(green_core_image(), [100, 100, float('nan'), 120])


def test_hud_filter_preserves_red_and_original_result():
  original = sample_result('green')
  image = frame((91, 131, 138))
  output = filter_for_hud(image, original)
  assert original['tracks'][0]['evidence']['raw'] == 'green'
  assert original['tracks'][0]['state'] == 'green'
  assert output['tracks'][0]['evidence']['raw'] == 'unknown'
  assert output['tracks'][0]['state'] == 'unknown'
  assert filter_for_hud(image, sample_result('red'))['tracks'][0]['evidence']['raw'] == 'red'
  assert filter_for_hud(green_core_image(), original)['tracks'][0]['evidence']['raw'] == 'green'


def test_display_feed_does_not_change_control_feed(tmp_path, monkeypatch):
  original = sample_result()
  control_path = tmp_path / 'control.json'
  hud_path = tmp_path / 'display.json'
  publish_observation(original, 1, 100., 'observer', path=control_path)
  publish_observation(filter_for_hud(frame((91, 131, 138)), original), 1, 100., 'observer', path=hud_path)
  assert json.loads(control_path.read_text())['tracks'][0]['evidence']['raw'] == 'green'
  assert json.loads(hud_path.read_text())['tracks'][0]['evidence']['raw'] == 'unknown'
  monkeypatch.setattr(hud_observations, 'SIGNAL_PATH', hud_path)
  preview = {'road': {'frame': 1, 'time': 100., 'source_width': 1344,
                      'source_height': 760, 'jpeg': 'image'}}
  display = hud_observations.signal_snapshot(100.1, preview)
  assert display['record']['tracks'][0]['raw'] == 'unknown'
  assert hud_observations.signal_snapshot(100.1, {})['record'] is None
