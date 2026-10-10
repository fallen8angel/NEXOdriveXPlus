"""Small optional inference badge, shared by Comma road displays."""
from openpilot.common.jetson_status import NativeJetlinkStatus


class JetsonBadge:
  def __init__(self):
    self.status = NativeJetlinkStatus()

  def render(self, rect, font, *, right_margin=24):
    badge = self.status.update()
    if badge is None:
      return
    import pyray as rl
    from openpilot.system.ui.lib.text_measure import measure_text_cached

    text, state = badge
    size = measure_text_cached(font, text, 22)
    box = rl.Rectangle(rect.x + rect.width - size.x - 20 - right_margin, rect.y + 12, size.x + 20, size.y + 10)
    rl.draw_rectangle_rounded(box, .25, 6, rl.Color(0, 0, 0, 150))
    color = rl.Color(0, 255, 0, 230) if state == 'active' else rl.Color(255, 255, 255, 210)
    rl.draw_text_ex(font, text, rl.Vector2(box.x + 10, box.y + 5), 22, 0, color)
