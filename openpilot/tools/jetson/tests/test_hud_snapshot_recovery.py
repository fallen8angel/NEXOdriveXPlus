"""Real panel wrapper and serial supervisor; no graphics or USB hardware."""
import json

import pytest

from openpilot.tools.jetson import hud, recovery
from openpilot.tools.jetson.snapshot import encode


def snapshot(onroad=True, debug=0):
  return {'params': {'IsOnroad': encode(onroad), 'ClusterHudDebug': encode(debug)}}


@pytest.mark.parametrize('value,expected', [
  (None, False), (snapshot(), True), (snapshot(False), False),
  (snapshot(False, 1), True), (snapshot(False, -1), False),
  ({'params': {'IsOnroad': '?'}}, False), ({'params': {'ClusterHudDebug': encode('broken')}}, False),
])
def test_admission_uses_live_snapshot_values(value, expected):
  assert hud.hud_requested(value) is expected


def test_snapshot_loss_stops_transmission_and_readmits_one_renderer(monkeypatch, tmp_path):
  sent, opened, released = [], [], []
  current, now = [snapshot()], [100.]
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(recovery, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: current[0])
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now[0])
  class Panel:
    def send_h264_chunk(self, packet):
      sent.append(packet)
      return len(packet)
  Display = hud.display_class(Panel)
  class Stop(BaseException):
    pass
  active = [0]
  def run():
    assert active[0] == 0
    active[0] += 1
    panel = Display()
    opened.append(panel)
    assert panel.send_h264_chunk(b'frame') == 5
    if len(opened) == 1:
      current[0] = None  # expired producer/USB lease during an active session
      panel.send_h264_chunk(b'must not send')
    else:
      raise Stop
  def release():
    assert active[0] == 1
    active[0] = 0
    released.append(True)
  def sleep(seconds):
    assert not (tmp_path / 'hud-status.json').exists()
    assert active[0] == 0
    record = json.loads((tmp_path / 'hud-recovery.json').read_text())
    assert 'snapshot expired' in record['last_error'] and record['failures'] == 1
    now[0] += seconds
    current[0] = snapshot()  # new comma session on the same locked supervisor
  with pytest.raises(Stop):
    recovery.run_hud_sessions(run, lambda: hud.hud_requested(current[0]), release,
                              clock=lambda: now[0], sleep=sleep)
  assert sent == [b'frame', b'frame'] and len(opened) == 2 and len(released) == 1
  assert not (tmp_path / 'hud-status.json').exists()


def test_offroad_stops_without_writing_panel_or_status(monkeypatch, tmp_path):
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: snapshot(False))
  class Panel:
    def send_h264_chunk(self, *args):
      pytest.fail('offroad snapshot sent to panel')
  with pytest.raises(SystemExit) as stopped:
    hud.display_class(Panel)().send_h264_chunk(b'frame')
  assert stopped.value.code == 0 and not (tmp_path / 'hud-status.json').exists()
