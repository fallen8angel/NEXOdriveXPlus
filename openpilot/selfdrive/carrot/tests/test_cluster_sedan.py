"""Render synthetic tracking states using the production Raylib mesh and lamp paths."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cluster'))
import pyray as rl
from cluster_renderer import ClusterUiRenderer
from cluster_scene import Vec3, translate_vehicle_box_x, vehicle_box


def tracked(x=0., y=0., mask=0):
  return vehicle_box(0., y, 0., 3.6, (84, 96, 110), True, source='radarState',
                     center_x_m_override=x, lamp_mask=mask)


def test_persistent_style_is_forwarded_and_honored():
  from cluster_config import CLUSTER_VEHICLE_STYLE_PARAM as key
  from main import ClusterRadarDisplayParamReader
  from openpilot.tools.jetson.snapshot import PARAMS

  catalog = Path(__file__).resolve().parents[2] / 'carrot_settings.json'
  settings = json.loads(catalog.read_text(encoding='utf-8'))
  entry, = [p for p in settings['params'] if p['name'] == key]
  assert (entry['min'], entry['max'], entry['default']) == (0, 1, 1)
  assert entry['options']['ko'] == ['기존 네모 박스', '2.5D 자동차']
  assert key in PARAMS
  reader = ClusterRadarDisplayParamReader.__new__(ClusterRadarDisplayParamReader)
  reader._params = {}
  assert reader.use_vehicle_model()
  for value, enabled in ((b'0', False), (b'1', True), (b'invalid', False)):
    reader._params[key] = value
    assert reader.use_vehicle_model() == enabled


def test_shift_keeps_only_the_associated_vehicle_lights():
  box = tracked(mask=5)
  assert translate_vehicle_box_x(box, 3.) == replace(box, center=Vec3(3., box.center.y, box.center.z))


def test_draw_failure_latches_box_without_changing_preference(monkeypatch):
  ui = object.__new__(ClusterUiRenderer)
  ui._vehicle_model = object()
  calls = []
  def fail(v):
    calls.append('mesh')
    raise RuntimeError('simulated draw failure')
  monkeypatch.setattr(ui, '_draw_vehicle_model', fail)
  monkeypatch.setattr(ui, '_draw_vehicle_marker', lambda v: calls.append(v.lamp_mask))
  ui._draw_vehicle(tracked(mask=2))
  ui._draw_vehicle(tracked(mask=4))
  assert calls == ['mesh', 2, 4] and ui.use_vehicle_model


@pytest.fixture
def graphics():
  if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
    pytest.skip('run under Xvfb or X11 for the OpenGL pixel tests')
  rl.set_config_flags(rl.FLAG_WINDOW_HIDDEN)
  rl.init_window(640, 360, 'HUD renderer regression')
  assert rl.is_window_ready()
  ui = object.__new__(ClusterUiRenderer)
  ui._vehicle_model = None
  ui._vehicle_model_load_attempted = False
  ui.profile_enabled = False
  target = rl.load_render_texture(640, 360)
  try:
    ui._load_vehicle_model()
    assert ui._vehicle_model is not None
    assert ui._vehicle_model.meshCount == 1
    assert ui._vehicle_model.meshes[0].triangleCount <= 256
    yield ui, target
  finally:
    if ui._vehicle_model is not None:
      rl.unload_model(ui._vehicle_model)
    rl.unload_render_texture(target)
    rl.close_window()


def capture(ui, target, vehicles, eye=(2.5, -6.8, 3.7), look=(0., 0., .65)):
  camera = rl.Camera3D(eye, look, (0, 0, 1), 34., rl.CAMERA_PERSPECTIVE)
  rl.begin_texture_mode(target)
  rl.clear_background((39, 45, 52, 255))
  rl.begin_mode_3d(camera)
  for vehicle in vehicles:
    ui._draw_vehicle(vehicle)
  rl.end_mode_3d()
  rl.end_texture_mode()
  img = rl.load_image_from_texture(target.texture)
  try:
    data = rl.ffi.buffer(img.data, img.width * img.height * 4)
    return np.frombuffer(data, np.uint8).reshape(img.height, img.width, 4)[::-1, :, :3].copy()
  finally:
    rl.unload_image(img)


def test_rendered_masks_heading_perspective_and_box_selection(graphics):
  ui, rt = graphics
  def frame(mask, **kwargs):
    return capture(ui, rt, [tracked(mask=mask)], **kwargs)
  off = frame(0)
  def pixels(image, color):
    return np.all(image == color, axis=2)
  red = (255, 18, 10)
  amber = (255, 186, 12)
  left = pixels(frame(2), amber)
  right = pixels(frame(4), amber)
  assert left.sum() > 10 and right.sum() > 10 and not (left & right).any()
  assert left.nonzero()[1].mean() < right.nonzero()[1].mean()
  assert not pixels(off, amber).any() and not pixels(off, red).any()
  brake = pixels(frame(1), red)
  assert brake.sum() > 20
  for mask, phase in ((1, np.zeros_like(left)), (3, left), (5, right), (8, left | right), (9, left | right)):
    image = frame(mask)
    assert np.array_equal(pixels(image, amber), phase)
    assert np.array_equal(pixels(image, red), brake if mask & 1 else np.zeros_like(brake))
  assert np.array_equal(frame(0), off)
  # Two tracks with independent phases in a single framebuffer.
  front_view = dict(eye=(0., -12., 5.), look=(0., 0., .65))
  cars = capture(ui, rt, [tracked(-2., mask=1), tracked(2., mask=4)], **front_view)
  assert pixels(cars, red).nonzero()[1].max() < pixels(cars, amber).nonzero()[1].min()
  near = capture(ui, rt, [tracked()], **front_view)
  far = capture(ui, rt, [tracked(y=8.)], **front_view)
  def area(image):
    return np.any(image != image[0, 0], axis=2).sum()
  assert 0 < area(far) < area(near) * .7
  reversed_car = replace(tracked(mask=1), right_x=-1., forward_y=-1.)
  assert not pixels(capture(ui, rt, [reversed_car], **front_view), red).any()
  ui.use_vehicle_model = False
  assert not np.array_equal(frame(0), off) and pixels(frame(3), red).any()
  ui.use_vehicle_model = True
  assert np.array_equal(frame(0), off)
