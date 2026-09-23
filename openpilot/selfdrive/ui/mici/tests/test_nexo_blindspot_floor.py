import ast
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest


ONROAD_PATH = Path(__file__).parents[1] / "onroad" / "__init__.py"
SHADER_PATH = Path(__file__).parents[4] / "system" / "ui" / "lib" / "shader_polygon.py"


def load_functions(path, names, namespace=None):
  tree = ast.parse(path.read_text(encoding="utf-8"))
  functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
  namespace = {} if namespace is None else namespace
  exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
  return namespace


def make_renderer(*, side, center=238.0, fallback=False, fingerprint="HYUNDAI_NEXO_1ST_GEN"):
  # Load the real NEXO patch without starting the UI, importing CAN bindings,
  # or registering its process-wide import hook.
  patch = load_functions(ONROAD_PATH, {"_is_nexo", "_patch_nexo_model"})["_patch_nexo_model"]
  triangulate = load_functions(SHADER_PATH, {"triangulate"}, {"np": np, "cast": cast})["triangulate"]
  triangles = []
  events = []

  class ModelRenderer:
    def _draw_lane_lines(self):
      events.append("lanes")

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
    # Interpret the existing shader's actual strip, including its odd-vertex
    # behavior, so the regression also exercises the old rendering path.
    strip = triangulate(points)
    events.append("warning")
    for i in range(2, len(strip)):
      indices = (i, i - 2, i - 1) if i % 2 == 0 else (i, i - 1, i - 2)
      triangles.append(np.array([strip[j] for j in indices]))

  state = SimpleNamespace(leftBlindspot=side in (-1, 2), rightBlindspot=side in (1, 2))
  module = SimpleNamespace(
    ModelRenderer=ModelRenderer, np=np, MIN_DRAW_DISTANCE=10.0, MAX_DRAW_DISTANCE=100.0,
    rl=SimpleNamespace(Color=lambda *args: args, draw_triangle_fan=draw_fan),
    ui_state=SimpleNamespace(CP=SimpleNamespace(carFingerprint=fingerprint), sm={"carState": state}),
    draw_polygon=draw_ribbon,
  )
  patch(module)
  renderer = ModelRenderer()
  renderer._rect = SimpleNamespace(x=0.0, y=0.0, width=476.0, height=240.0)
  renderer._car_space_transform = np.array([[center, 180.0, 0.0], [80.0, 0.0, 180.0], [1.0, 0.0, 0.0]], dtype=np.float32)
  xs = np.array([0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 32.0], dtype=np.float32)

  def line(lateral):
    return SimpleNamespace(raw_points=np.column_stack((xs, np.full_like(xs, lateral), np.full_like(xs, 1.22))))

  renderer._path = line(0.0)
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
    # Raylib expects counter-clockwise vertices (negative area with screen Y
    # pointing down). A mirrored face must not be lost to back-face culling.
    if cross(a, b, c) >= -1e-6:
      continue
    inside = (cross(a, b, (x, y)) <= 0) & (cross(b, c, (x, y)) <= 0) & (cross(c, a, (x, y)) <= 0)
    mask |= inside
  return mask


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("center", [200.0, 238.0, 280.0])
@pytest.mark.parametrize("fallback", [False, True])
def test_warning_fills_visible_lane_to_screen_edge(side, center, fallback):
  renderer, triangles, events = make_renderer(side=side, center=center, fallback=fallback)
  renderer._draw_lane_lines()

  # Independent pinhole-camera oracle for a flat, straight lane: its inner
  # and outer edges are straight lines, even when most of the near face is
  # outside the viewport. Check every visible pixel, not just draw calls.
  y, x = np.mgrid[:240, :476] + 0.5
  inner = center + side * 1.81 / 1.22 * (y - 80.0)
  outer = center + side * 4.6 / 1.22 * (y - 80.0)
  expected = (y >= 80.0 + 180.0 * 1.22 / 32.0) & (x >= np.minimum(inner, outer)) & (x <= np.maximum(inner, outer))
  expected &= x < 238.0 if side < 0 else x >= 238.0
  np.testing.assert_array_equal(coverage(triangles), expected)
  assert events[-1] == "lanes"


def test_left_and_right_warning_faces_are_mirrored():
  masks = []
  for side in (-1, 1):
    renderer, triangles, _ = make_renderer(side=side)
    renderer._draw_lane_lines()
    masks.append(coverage(triangles))
  assert masks[0].any()
  np.testing.assert_array_equal(masks[0], masks[1][:, ::-1])


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
