import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.ui.jetson_badge import JetsonBadge

UI = Path(__file__).resolve().parents[1]


def rectangle(x, y, width, height):
  return SimpleNamespace(x=x, y=y, width=width, height=height)


@pytest.mark.parametrize('state', ['ready', 'active', None])
def test_badge_draws_only_verified_state_inside_road_rect(monkeypatch, state):
  texts, boxes = [], []
  monkeypatch.setitem(sys.modules, 'pyray', SimpleNamespace(
    Rectangle=rectangle, Color=lambda *rgba: rgba, Vector2=lambda x, y: (x, y),
    draw_rectangle_rounded=lambda box, *args: boxes.append(box),
    draw_text_ex=lambda font, text, pos, size, spacing, color: texts.append((text, pos, color)),
  ))
  monkeypatch.setitem(sys.modules, 'openpilot.system.ui.lib.text_measure',
                      SimpleNamespace(measure_text_cached=lambda *a: SimpleNamespace(x=150, y=22)))
  badge = JetsonBadge()
  label = ('JETSON', 'active') if state == 'active' else ('JETSON READY', 'ready')
  monkeypatch.setattr(badge.status, 'update', lambda: label if state else None)
  rect = rectangle(40, 60, 900, 600)
  badge.render(rect, 'font', right_margin=120)
  if state is None:
    assert not texts and not boxes
  else:
    assert texts[0][0] == label[0]
    assert texts[0][2] == ((0, 255, 0, 230) if state == 'active' else (255, 255, 255, 210))
    assert boxes[0].x + boxes[0].width == rect.x + rect.width - 120
    assert boxes[0].y >= rect.y


def test_home_native_connection_survives_unavailable_udp_receiver(monkeypatch):
  path = UI / 'mici/layouts/home.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'JetsonStatusReceiver')
  namespace = {'time': SimpleNamespace(monotonic=lambda: 10), 'JETSON_STATUS_STALE_SECONDS': 2.5}
  exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), namespace)
  receiver = namespace['JetsonStatusReceiver']()
  receiver._open_socket = lambda now: None
  monkeypatch.setattr(receiver._native, 'update', lambda now: ('JETSON READY', 'ready'))
  assert receiver.connected()
  monkeypatch.setattr(receiver._native, 'update', lambda now: None)
  assert not receiver.connected()


@pytest.mark.parametrize('layout', ['mici/onroad', 'onroad'])
def test_both_road_renderers_retain_existing_draws_and_add_badge(layout):
  path = UI / layout / 'hud_renderer.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudRenderer')
  method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_render')
  method.args.args[1].annotation = None
  method.returns = None
  calls = []
  renderer = SimpleNamespace(
    _jetson_badge=SimpleNamespace(render=lambda *a, **kw: calls.append(('badge', kw))),
    _font_semi_bold='font', _font_display='font', _show_plot_mode=0, is_cruise_available=True,
    _exp_button=SimpleNamespace(render=lambda rect: calls.append(('button', {}))),
    _plot_renderer=SimpleNamespace(draw=lambda *a: None),
  )
  for name in ('_refresh_hud_params', '_draw_set_speed_carrot', '_draw_date_time', '_draw_tpms', '_draw_set_speed',
               '_draw_steering_wheel', '_draw_cruise_speed_animation', '_draw_experimental_mode'):
    setattr(renderer, name, lambda *a, label=name: calls.append((label, {})))
  namespace = {'time': SimpleNamespace(monotonic=lambda: 10),
               'rl': SimpleNamespace(Rectangle=rectangle, draw_rectangle_gradient_v=lambda *a: None),
               'UI_CONFIG': SimpleNamespace(header_height=300, border_size=24, button_size=80),
               'COLORS': SimpleNamespace(HEADER_GRADIENT_START=None, HEADER_GRADIENT_END=None)}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
  namespace['_render'](renderer, rectangle(0, 0, 900, 600))
  assert sum(name == 'badge' for name, _ in calls) == 1
  assert any(name == '_draw_cruise_speed_animation' for name, _ in calls)
  if layout == 'onroad':
    assert calls[-1] == ('badge', {'right_margin': 116})
    assert any(name == 'button' for name, _ in calls)
