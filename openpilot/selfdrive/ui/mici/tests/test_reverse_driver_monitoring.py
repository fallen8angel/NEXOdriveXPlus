import ast
from pathlib import Path


OPENPILOT_ROOT = Path(__file__).resolve().parents[4]


def read_python(path: str) -> str:
  source = (OPENPILOT_ROOT / path).read_text(encoding="utf-8")
  ast.parse(source, filename=path)
  return source


def load_function(path: str, name: str):
  source = read_python(path)
  tree = ast.parse(source, filename=path)
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
  module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
  namespace = {"Params": object}
  exec(compile(module, path, "exec"), namespace)
  return namespace[name]


def test_reverse_never_opens_driver_camera_or_preview_mode():
  main = read_python("selfdrive/ui/mici/layouts/main.py")
  daemon = read_python("selfdrive/monitoring/dmonitoringd.py")

  for token in ("DriverCameraDialog", "ReverseDriverCamera", "reverse_camera"):
    assert token not in main

  for token in (
    "def driver_view_demo_mode",
    'get_bool("IsOffroad")',
    'not params.get_bool("IsOnroad")',
    "not actual_vehicle_state_valid",
    "DM.run_step(sm, demo=False)",
  ):
    assert token in daemon
  assert "DM.run_step(sm, demo=demo_mode)" not in daemon


def test_driver_view_demo_mode_is_offroad_only():
  demo_mode = load_function("selfdrive/monitoring/dmonitoringd.py", "driver_view_demo_mode")

  class FakeParams:
    def __init__(self, **values):
      self.values = values

    def get_bool(self, key):
      return self.values.get(key, False)

  assert demo_mode(FakeParams(IsDriverViewEnabled=True, IsOffroad=True, IsOnroad=False))
  assert not demo_mode(FakeParams(IsDriverViewEnabled=True, IsOffroad=False, IsOnroad=True))
  assert not demo_mode(FakeParams(IsDriverViewEnabled=True, IsOffroad=True, IsOnroad=True))
  assert not demo_mode(FakeParams(IsDriverViewEnabled=False, IsOffroad=True, IsOnroad=False))


def test_reverse_hides_dmoji_and_blocks_stale_alerts():
  mici_driver_state = read_python("selfdrive/ui/mici/onroad/driver_state.py")
  tici_driver_state = read_python("selfdrive/ui/onroad/driver_state.py")
  augmented_view = read_python("selfdrive/ui/mici/onroad/augmented_road_view.py")
  selfdrived = read_python("selfdrive/selfdrived/selfdrived.py")

  for source in (mici_driver_state, tici_driver_state, augmented_view):
    assert 'gearShifter != car.CarState.GearShifter.reverse' in source

  assert "in_drive_gear = CS.gearShifter in" in selfdrived
  assert "and in_drive_gear:" in selfdrived
