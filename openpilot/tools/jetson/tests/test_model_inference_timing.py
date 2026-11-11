"""Validate timing extraction without starting modeld, USB or TensorRT."""
import ast
import math
from pathlib import Path

from openpilot.tools.jetson import manage


def _timing_helper():
  path = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/jetlink/model.py'
  tree = ast.parse(path.read_text())
  function = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                  and item.name == 'inference_timing_sample')
  scope = {'math': math}
  exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), scope)
  return scope['inference_timing_sample']


def test_model_inference_timing_only_from_completed_valid_reply():
  timing = _timing_helper()
  value = timing([12000, 3000, 17500], .0233, 105.)
  assert value['modeld_roundtrip_ms'] == 23.3
  assert value['jetson_execution_us'] == 12000
  assert value['jetson_total_us'] == 17500
  assert value['last_inference_at_mono'] == 105.
  for raw, elapsed in (((), .03), ((1, 2), .02), ((1, 2, -3), .02),
                       ((1, float('nan'), 3), .02), ((1, 2, 3), -1),
                       ((1, 2, 3), float('inf'))):
    assert timing(raw, elapsed, 100.) is None


def test_performance_is_only_new_read_only_remote_action():
  assert 'performance' in manage.ACTIONS
  assert 'enable_ai' not in manage.ACTIONS
  assert 'build_engine' not in manage.ACTIONS
