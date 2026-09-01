import ast
from pathlib import Path
from types import SimpleNamespace


AUGMENTED_ROAD_VIEW_PATH = Path(__file__).parents[1] / "onroad" / "augmented_road_view.py"


def load_layout_function():
  tree = ast.parse(AUGMENTED_ROAD_VIEW_PATH.read_text(encoding="utf-8"))
  function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "driver_monitoring_position"
  )
  module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
  namespace = {}
  exec(compile(module, str(AUGMENTED_ROAD_VIEW_PATH), "exec"), namespace)
  return namespace["driver_monitoring_position"]


def test_driver_monitoring_icon_is_right_of_fixed_wheel():
  position = load_layout_function()
  wheel = SimpleNamespace(x=18, y=18, width=50, height=50)

  x, y = position(wheel, 60)

  assert (x, y) == (76, 13)
  assert x - (wheel.x + wheel.width) == 8
  assert y + 60 / 2 == wheel.y + wheel.height / 2
