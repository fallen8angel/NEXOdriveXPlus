"""Protect the optional observation reader in the shipped Jetson HUD runtime."""
import ast
from pathlib import Path
import runpy
from types import SimpleNamespace

from openpilot.tools.jetson.build_protected_bundle import PATHS


def test_renderer_observation_dependency_is_exported_and_works_without_vehicle_extensions(tmp_path):
  root = Path(__file__).resolve().parents[4]
  renderer = ast.parse((root / 'openpilot/selfdrive/carrot/cluster/cluster_renderer.py').read_text(encoding='utf8'))
  imports = [node for node in ast.walk(renderer) if isinstance(node, ast.ImportFrom)
             and any(name.name == 'BrakeDisplayReader' for name in node.names)]
  assert len(imports) == 1
  dependency = imports[0].module.replace('.', '/') + '.py'
  assert any(dependency == item or dependency.startswith(item + '/') for item in PATHS), dependency
  # Load only the exported reader module, without cereal/Params/VisionIPC or a detector.
  exported = tmp_path / 'lead_brake_lights.py'
  exported.write_bytes((root / dependency).read_bytes())
  reader = runpy.run_path(str(exported))['BrakeDisplayReader']()
  assert reader.read(SimpleNamespace(get_bool=lambda key: False), now=100.) is None
  record = dict(version=1, timestamp=100., frame_id=1, state='candidate', control_permission=False,
                stream='road', width=1344, height=760, box=None)
  params = SimpleNamespace(get_bool=lambda key: True, lead_brake_observation=lambda: record)
  record['timestamp'] = 101.
  assert reader.read(params, now=101.)['state'] == 'candidate'
  assert reader.read(params, now=101.3) is None
