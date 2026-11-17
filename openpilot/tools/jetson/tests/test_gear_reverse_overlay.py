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
