import ast
import itertools
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest


OPENPILOT = Path(__file__).parents[4]
ONROAD_PATH = Path(__file__).parents[1] / "onroad" / "__init__.py"
SHADER_PATH = OPENPILOT / "system" / "ui" / "lib" / "shader_polygon.py"
CAMERA_PATH = OPENPILOT / "common" / "transformations" / "camera.py"
VIEW_PATH = ONROAD_PATH.with_name("augmented_road_view.py")


def load_source(path, names=None, namespace=None):
  tree = ast.parse(path.read_text(encoding="utf-8"))
  if names is None:
    nodes = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
  else:
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
  namespace = {} if namespace is None else namespace
  exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
  return namespace


def make_renderer(*, side, sensor="os04c10", wide=False, fallback=False, curve=0.0, fingerprint="HYUNDAI_NEXO_1ST_GEN"):
  # Isolate CAN/GUI services, but execute the repository's actual NEXO patch,
  # camera configuration, frame matrix, and model sampling distances.
  patch = load_source(ONROAD_PATH, {"_is_nexo", "_patch_nexo_model"})["_patch_nexo_model"]
  triangulate = load_source(SHADER_PATH, {"triangulate"}, {"np": np, "cast": cast})["triangulate"]
  triangles = []
  events = []

  class ModelRenderer:
    def _draw_lane_lines(self):
      events.append("lanes")

    def set_transform(self, transform):
      self._car_space_transform = transform.astype(np.float32)

    @staticmethod
    def _get_path_length_idx(xs, distance):
      indices = np.flatnonzero(xs <= distance)
      return indices[-1] if indices.size else 0

  def draw_fan(points, count, color):
    assert len(points) == count
    assert color == (255, 0, 0, 175)
    events.append("warning")
    triangles.extend(np.array([points[0], points[i], points[i + 1]]) for i in range(1, count - 1))

  def draw_ribbon(_rect, points, color):
    strip = triangulate(points)
    events.append("warning")
    for i in range(2, len(strip)):
      indices = (i, i - 2, i - 1) if i % 2 == 0 else (i, i - 1, i - 2)
      triangles.append(np.array([strip[j] for j in indices]))

  state = SimpleNamespace(leftBlindspot=side in (-1, 2), rightBlindspot=side in (1, 2), vEgo=60 / 3.6)

  class SubMaster(dict):
    recv_frame = {"liveCalibration": 100}

  ui_state = SimpleNamespace(CP=SimpleNamespace(carFingerprint=fingerprint), sm=SubMaster(carState=state))
  module = SimpleNamespace(
    ModelRenderer=ModelRenderer, np=np, MIN_DRAW_DISTANCE=10.0, MAX_DRAW_DISTANCE=100.0,
    rl=SimpleNamespace(Color=lambda *args: args, draw_triangle_fan=draw_fan),
    ui_state=ui_state, draw_polygon=draw_ribbon,
  )
  patch(module)
  renderer = ModelRenderer()
  renderer._rect = SimpleNamespace(x=0.0, y=0.0, width=476.0, height=240.0)
  renderer._path_offset_z = 1.22

  cameras = load_source(CAMERA_PATH, namespace={"np": np, "dataclass": dataclass, "itertools": itertools})
  camera = cameras["DEVICE_CAMERAS"]["mici", sensor]
  tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"))
  view_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AugmentedRoadView")
  method = next(node for node in view_class.body if isinstance(node, ast.FunctionDef) and node.name == "_calc_frame_matrix")
  namespace = {"np": np, "rl": SimpleNamespace(Rectangle=object), "ui_state": ui_state,
               "WIDE_CAM": 1, "CAM_Y_OFFSET": 20, "DEFAULT_DEVICE_CAMERA": camera}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(VIEW_PATH), "exec"), namespace)
  view = SimpleNamespace(_content_rect=renderer._rect, device_camera=camera, stream_type=int(wide),
                         view_from_calib=cameras["view_frame_from_device_frame"],
                         view_from_wide_calib=cameras["view_frame_from_device_frame"], _model_renderer=renderer)
  namespace["_calc_frame_matrix"](view, renderer._rect)

  constants = load_source(OPENPILOT / "selfdrive" / "modeld" / "constants.py", namespace={"np": np})
  xs = np.array(constants["ModelConstants"].X_IDXS, dtype=np.float32)

  def line(lateral, height=1.22):
    return SimpleNamespace(raw_points=np.column_stack((xs, curve * xs**2 + lateral, np.full_like(xs, height))))

  # Model position is at the camera origin; lane lines are on the ground.
  renderer._path = line(0.0, 0.0)
  renderer._lane_lines = [line(-5.4), line(-1.8), line(1.8), line(5.4)]
  if fallback:
    for lane in renderer._lane_lines:
      lane.raw_points = np.empty((0, 3), dtype=np.float32)
  return renderer, triangles, events


def cross(a, b, c):
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def coverage(triangles):
  y, x = np.mgrid[:240, :476] + 0.5
  mask = np.zeros(x.shape, dtype=bool)
  for a, b, c in triangles:
    # Counter-clockwise with screen Y pointing down.
    if cross(a, b, c) >= -1e-6:
      continue
    mask |= (cross(a, b, (x, y)) <= 0) & (cross(b, c, (x, y)) <= 0) & (cross(c, a, (x, y)) <= 0)
  return mask


@pytest.mark.parametrize("side", [-1, 1, 2])
@pytest.mark.parametrize("sensor,wide", [("os04c10", False), ("ar0231", False), ("os04c10", True)])
@pytest.mark.parametrize("fallback", [False, True])
def test_warning_covers_full_visible_lane_at_actual_mici_zoom(side, sensor, wide, fallback):
  renderer, triangles, events = make_renderer(side=side, sensor=sensor, wide=wide, fallback=fallback)
  renderer._draw_lane_lines()

  # Independent analytic oracle: a straight, flat lane projects to two lines.
  # OPKR fills from boundary - 0.01 to boundary +/- 2.8 for the full model
  # range. Testing only 0..32 m at a made-up 180 px focal length hid the bug.
  y, x = np.mgrid[:240, :476] + 0.5
  transform = renderer._car_space_transform
  center, horizon, focal = float(transform[0, 0]), float(transform[1, 0]), float(transform[1, 2])
  xs = renderer._path.raw_points[:, 0]
  far = float(xs[xs <= 100.0][-1])
  expected = np.zeros(x.shape, dtype=bool)
  for direction in ((-1, 1) if side == 2 else (side,)):
    inner = center + (direction * 1.8 - 0.01) / 1.22 * (y - horizon)
    outer = center + direction * 4.6 / 1.22 * (y - horizon)
    expected |= (y >= horizon + focal * 1.22 / far) & (x >= np.minimum(inner, outer)) & (x <= np.maximum(inner, outer))
  np.testing.assert_array_equal(coverage(triangles), expected)
  assert events[-1] == "lanes"


@pytest.mark.parametrize("side,curve", [(-1, 0.001), (1, -0.001)])
def test_curved_lane_is_not_cut_at_screen_center(side, curve):
  renderer, triangles, _ = make_renderer(side=side, curve=curve)
  renderer._draw_lane_lines()
  actual = coverage(triangles)
  # A lane on a bend can cross screen center. Its physical side is encoded by
  # the model lane index; a hard screen-half mask must not remove that part.
  opposite_half = actual[:, 238:] if side < 0 else actual[:, :238]
  assert opposite_half.sum() > 100


@pytest.mark.parametrize("side,fingerprint", [(0, "HYUNDAI_NEXO_1ST_GEN"), (2, "OTHER_CAR")])
def test_warning_remains_gated_by_sensor_and_vehicle(side, fingerprint):
  renderer, triangles, events = make_renderer(side=side, fingerprint=fingerprint)
  renderer._draw_lane_lines()
  assert triangles == []
  assert events == ["lanes"]


def test_invalid_projection_does_not_draw_warning():
  renderer, triangles, events = make_renderer(side=2)
  renderer._car_space_transform[2] = (-1.0, 0.0, 0.0)
  renderer._draw_lane_lines()
  assert triangles == []
  assert events == ["lanes"]
