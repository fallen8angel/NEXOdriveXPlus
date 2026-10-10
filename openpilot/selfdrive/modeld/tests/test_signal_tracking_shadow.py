from types import SimpleNamespace
import ast
from pathlib import Path

import numpy as np
import pytest

from openpilot.selfdrive.modeld.signal_tracking_shadow import copy_nv12_rgb, next_delay, result_fields, tracking_requested


def test_tracking_is_explicit_opt_in(tmp_path):
  assert not tracking_requested(tmp_path)
  (tmp_path / 'tracking_enabled').write_text('1')
  assert tracking_requested(tmp_path)
  (tmp_path / 'tracking_enabled').write_text('0')
  assert not tracking_requested(tmp_path)


@pytest.mark.parametrize('age', [-1, 201, float('nan')])
def test_stale_output_cannot_be_green(age):
  result = result_fields({'state': 'green', 'reason': 'agreeing_visible_tracks'}, age)
  assert result['prediction'] == 'unknown' and not result['fresh'] and not result['control_permission']


@pytest.mark.parametrize('wall,cpu', [(0.01, 0.01), (0.08, 0.04), (0.3, 0.2)])
def test_cpu_and_rate_budget(wall, cpu):
  delay = next_delay(wall, cpu)
  assert wall + delay >= 0.05
  assert cpu / (cpu + delay) <= 0.5


def test_nv12_padding_and_no_input_mutation():
  width, height, stride = 1344, 760, 1408
  offset = stride * height + 128
  raw = np.full(offset + stride * height // 2, 99, np.uint8)
  y = raw[: stride * height].reshape(height, stride)
  uv = raw[offset:].reshape(height // 2, stride)
  y[:, :width] = 16
  y[:, width // 2 : width] = 235
  uv[:, :width] = 128
  before = raw.copy()
  frame = SimpleNamespace(width=width, height=height, stride=stride, uv_offset=offset, data=raw)
  rgb = copy_nv12_rgb(frame)
  assert rgb.shape == (height, width, 3)
  assert (rgb[:, : width // 2] == 0).all() and (rgb[:, width // 2 :] == 255).all()
  np.testing.assert_array_equal(raw, before)
  frame.data = raw[:-1]
  with pytest.raises(ValueError, match='truncated'):
    copy_nv12_rgb(frame)
  frame.width = 1008
  with pytest.raises(ValueError, match='geometry'):
    copy_nv12_rgb(frame)


def test_existing_worker_dispatches_only_with_opt_in(monkeypatch, tmp_path):
  from openpilot.selfdrive.modeld import signal_color_shadow, signal_tracking_shadow

  calls = []
  (tmp_path / 'tracking_enabled').write_text('1')
  monkeypatch.setattr(signal_tracking_shadow, 'run', lambda directory, duration: calls.append((directory, duration)))
  signal_color_shadow.run(tmp_path, 2)
  assert calls == [(tmp_path, 2)]


def test_manager_observer_registration_requires_onroad_and_explicit_flag(monkeypatch, tmp_path):
  from openpilot.selfdrive.modeld import signal_color_shadow

  monkeypatch.setattr(signal_color_shadow, 'requested', lambda: (tmp_path / 'enabled').exists())
  path = Path(__file__).resolve().parents[3] / 'system/manager/process_config.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  gate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'signal_color_observer')
  ns = {'Params': object, 'car': SimpleNamespace(CarParams=object)}
  exec(compile(ast.Module([gate], type_ignores=[]), str(path), 'exec'), ns)
  call = next(
    n
    for n in ast.walk(tree)
    if isinstance(n, ast.Call)
    and isinstance(n.func, ast.Name)
    and n.func.id == 'PythonProcess'
    and n.args
    and isinstance(n.args[0], ast.Constant)
    and n.args[0].value == 'signalcolord'
  )
  ns['PythonProcess'] = lambda *a, **kw: (a, kw)
  args, kwargs = eval(compile(ast.Expression(call), str(path), 'eval'), ns)
  assert kwargs == {'spawn': True}
  assert args[:2] == ('signalcolord', 'openpilot.selfdrive.modeld.signal_color_shadow')
  assert not args[2](True, None, None)
  (tmp_path / 'enabled').write_text('1')
  assert args[2](True, None, None)
  assert not args[2](False, None, None)
