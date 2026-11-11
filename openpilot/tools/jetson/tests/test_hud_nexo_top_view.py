"""NEXO reverse-view artwork must stay lightweight and independent of camera/CAN."""

from types import SimpleNamespace

import pytest

from openpilot.tools.jetson.gear_view import draw_nexo_top_view


@pytest.mark.parametrize("width,length", [(45.12, 87.6), (37.6, 73.0)])
def test_nexo_overhead_car_uses_only_scaled_2d_primitives(width, length):
  rectangles, strokes = [], []
  rl = SimpleNamespace(
    Rectangle=lambda x, y, w, h: (x, y, w, h),
    Vector2=lambda x, y: (x, y),
    draw_rectangle_rounded=lambda box, radius, segments, color: rectangles.append((box, radius, segments, color)),
    draw_line_ex=lambda start, end, thick, color: strokes.append((start, end, thick, color)),
  )

  draw_nexo_top_view(rl, 200, 120, width, length)

  # No texture loads, encoder reconfiguration or sensor/CAN calls are needed.
  assert 17 <= len(rectangles) <= 40
  assert 10 <= len(strokes) <= 24
  assert all(0 < box[2] <= width and 0 < box[3] <= length for box, _, _, _ in rectangles)
  assert all(0 <= radius <= 1 and segments >= 4 for _, radius, segments, _ in rectangles)
  assert any(color[0] > 200 and color[1] < 90 and color[2] < 90 for *_, color in strokes)
  assert any(color[0] < 50 and color[2] > color[0] for *_, color in rectangles)
  assert all(0 < thickness <= width * .1 for _, _, thickness, _ in strokes)
