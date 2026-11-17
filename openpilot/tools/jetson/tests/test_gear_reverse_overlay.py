"""Reverse HUD must hide only the duplicate gear status info alert."""
from types import SimpleNamespace
from unittest.mock import Mock

from openpilot.tools.jetson.gear_view import GearView, redundant_reverse_gear_notice


def alert(title, detail='', status=0):
  return SimpleNamespace(text1=title, text2=detail, status=status, size=2)


def test_exact_reverse_gear_status_notice_is_redundant_but_safety_alerts_are_not():
  assert redundant_reverse_gear_notice(alert('기어 [R] 상태'))
  assert redundant_reverse_gear_notice(alert('기어   [R]   상태'))
  assert not redundant_reverse_gear_notice(alert('기어 [P] 상태'))
  assert not redundant_reverse_gear_notice(alert('기어 [R] 상태', '추가 경고: 장애물', 2))
  assert not redundant_reverse_gear_notice(alert('후방 충돌 위험', '', 2))
  assert not redundant_reverse_gear_notice(None)


def test_reverse_render_does_not_draw_duplicate_notice_over_parking_car():
  view = GearView()
  view.selector.update = Mock(return_value=('reverse', True))
  view.draw_reverse = Mock()
  renderer = SimpleNamespace(
    _side_camera=SimpleNamespace(close=Mock()),
    _reverse_camera=SimpleNamespace(close=Mock()),
    _draw_alert_overlay=Mock(),
  )
  fallback = Mock()
  messages = [alert('기어 [R] 상태'),
              alert('후방 충돌 위험', '', 2),
              alert('기어 [R] 상태', '다른 경고', 2)]
  for message in messages:
    renderer._draw_alert_overlay.reset_mock()
    view.render(renderer, None, fallback, SimpleNamespace(alert=message), None,
                {'gear_view': {}}, None, 100.)
    view.draw_reverse.assert_called()
    if message is messages[0]:
      renderer._draw_alert_overlay.assert_not_called()
    else:
      renderer._draw_alert_overlay.assert_called_once_with(message)
  fallback.assert_not_called()



def test_reverse_split_uses_two_equal_screen_halves_at_all_hud_resolutions():
  """The blue user sketch: camera wholly left of midpoint, parking wholly right."""
  from openpilot.tools.jetson.gear_view import reverse_split_geometry
  for width, height in ((960, 240), (1920, 480), (1280, 320)):
    camera, parking, split_x = reverse_split_geometry(width, height)
    assert split_x == width / 2.
    assert camera[0] >= 0
    assert camera[0] + camera[2] <= split_x
    assert parking[0] >= split_x
    assert parking[0] + parking[2] <= width
    # The Comma4 interior camera is 16:9, with no width distortion or cropping.
    assert abs(camera[2] / camera[3] - 16 / 9) < .05
    assert camera[1] >= 0 and camera[1] + camera[3] <= height
    assert parking[1] >= 0 and parking[1] + parking[3] <= height


def test_reverse_screen_places_camera_and_parking_panel_on_opposite_sides(monkeypatch):
  from openpilot.tools.jetson import gear_view

  rectangle = lambda x, y, width, height: SimpleNamespace(x=x, y=y, width=width, height=height)
  rl = SimpleNamespace(
    Rectangle=rectangle, Vector2=lambda x, y: SimpleNamespace(x=x, y=y),
    clear_background=Mock(), draw_rectangle=Mock(), draw_line_ex=Mock())
  monkeypatch.setattr(gear_view, 'draw_nexo_top_view', Mock())
  camera = SimpleNamespace(draw=Mock(return_value=True))
  text = Mock()
  ui = SimpleNamespace(width=960, height=240, _reverse_camera=camera, _draw_text=text)
  state = SimpleNamespace(parking_indications=None, steering_angle_deg=0, speed_kph=0)

  gear_view.GearView().draw_reverse(ui, rl, state, None, True, 100.0)

  destination = camera.draw.call_args.args[0]
  assert camera.draw.call_args.kwargs == {'fit': True, 'mirror': True}
  assert destination.x + destination.width < ui.width / 2
  assert 0 < destination.width and destination.height > 0
  bg = [call for call in rl.draw_rectangle.call_args_list
        if call.args[-1] == gear_view.REVERSE_PANEL_BG]
  assert len(bg) == 1
  x, y, width, height, _ = bg[0].args
  assert x >= ui.width / 2
  assert x + width <= ui.width
  assert abs(width - ui.width / 2) < ui.width * .02
  # Keep the big left R, the left interior-camera title, and all right-panel widgets.
  labels = [call.args[0] for call in text.call_args_list]
  assert 'R' in labels and '후진' in labels and '콤마4 실내 카메라' in labels
  assert '주차 보조 · R단' in labels
  assert '전방' in labels and '후방' in labels
  assert '핸들' in labels and '속도' in labels
  assert '기어 [R] 상태' not in labels
