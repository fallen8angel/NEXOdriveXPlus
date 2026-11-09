"""Real panel wrapper and serial supervisor; no graphics or USB hardware."""
import json
from contextlib import contextmanager

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


def test_snapshot_loss_blanks_vehicle_pixels_without_reopening_panel(monkeypatch, tmp_path):
  sent = []
  current, now = [{**snapshot(), 'updated': 100.}], [100.]
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(recovery, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: current[0])
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now[0])
  class Panel:
    def send_h264_chunk(self, packet):
      sent.append(packet)
      return len(packet)
  panel = hud.display_class(Panel)()
  vehicle = bytes((240, 0, 0, 255)) * (320 * 80)
  @contextmanager
  def capture():
    yield vehicle, 320, 80
  guarded = hud.guarded_capture(capture)
  for live in (True, False, False, True):
    now[0] += .6
    current[0] = {**snapshot(), 'updated': now[0]} if live else None
    with guarded() as (raw, _, _):
      assert (raw == vehicle) is live
      panel.send_h264_chunk(raw)  # Encoded submission is covered by FFmpeg test.
  assert len(sent) == 4 and sent[0] == sent[-1] == vehicle
  assert sent[1] == sent[2] != vehicle
  record = json.loads((tmp_path / 'hud-status.json').read_text())
  assert record['display_mode'] == 'vehicle'
  assert record['input_gap']['event'] == 'snapshot_expired'



def test_slow_render_uses_visible_waiting_frame_instead_of_solid_black(monkeypatch, tmp_path):
  from PIL import Image

  now = [100.]
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: {**snapshot(), 'updated': now[0]})
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now[0])
  width, height = 320, 80
  vehicle = bytes((240, 0, 0, 255)) * (width * height)

  @contextmanager
  def slow_capture():
    now[0] += .6
    yield vehicle, width, height

  guarded = hud.guarded_capture(slow_capture)
  with guarded() as (raw, got_width, got_height):
    assert (got_width, got_height) == (width, height)
    assert raw == hud.waiting_frame(width, height, False)
    assert raw != vehicle
    assert raw != bytes((0, 0, 0, 255)) * (width * height)
    img = Image.frombytes('RGBA', (width, height), raw)
    assert img.getpixel((0, 0)) == (36, 49, 61, 255)
    assert hud.RENDER_TIMING['render_expired'] is True
    assert hud.RENDER_TIMING['display_mode'] == 'waiting_data'



def test_capture_preserves_valid_short_lease_and_still_guards_expiry(monkeypatch, tmp_path):
  """Valid pixels keep their original expiry through encoding."""
  now = [100.]
  source = {'updated': now[0] - .338, **snapshot()}
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: source)
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now[0])
  width, height = 320, 80
  vehicle = bytes((245, 15, 0, 255)) * (width * height)

  @contextmanager
  def capture():
    yield vehicle, width, height

  guarded = hud.guarded_capture(capture)
  with guarded() as (raw, w, h):
    assert raw == vehicle  # Previously blanked with the 200 ms guard.
    assert hud.RENDER_TIMING['display_mode'] == 'vehicle'
    assert hud.RENDER_TIMING['frame_expires_mono'] == source['updated'] + .5
  source['updated'] = now[0] - .43
  with guarded() as (raw, w, h):
    assert raw == vehicle
    assert hud.RENDER_TIMING['display_mode'] == 'vehicle'
    assert 0 < hud.RENDER_TIMING['encode_lease_remaining_s'] < .1
    assert hud.RENDER_TIMING['frame_expires_mono'] == source['updated'] + .5

  # New data must immediately return to normal rendering without reopening
  # the panel or changing the source lease.
  now[0] += .1
  source['updated'] = now[0]
  with guarded() as (raw, w, h):
    assert raw == vehicle
    assert hud.RENDER_TIMING['display_mode'] == 'vehicle'
    assert hud.RENDER_TIMING['frame_expires_mono'] == source['updated'] + .5

  # Actual expiry always hides dynamic vehicle pixels even without a 100 ms
  # pre-encode reserve decision.
  source['updated'] = now[0] - .51
  with guarded() as (raw, w, h):
    assert raw == hud.waiting_frame(w, h, False)
    assert hud.RENDER_TIMING['display_mode'] == 'waiting_data'
    assert hud.RENDER_TIMING['frame_expires_mono'] is None
  assert json.loads((tmp_path / 'hud-input.json').read_text())['last_gap']['event'] == 'snapshot_expired'


def test_ffmpeg_thread_count_is_bounded_like_carrot():
  # Static so tests never start a real Jetson renderer/USB panel.
  from pathlib import Path
  source = (Path(__file__).resolve().parents[1] / 'hud.py').read_text()
  assert "'--usb-h264-ffmpeg-threads', '1'" in source
  assert '--fps\', \'10\'' in source

def test_offroad_stops_without_writing_panel_or_status(monkeypatch, tmp_path):
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: snapshot(False))
  class Panel:
    def send_h264_chunk(self, *args):
      pytest.fail('offroad snapshot sent to panel')
  with pytest.raises(SystemExit) as stopped:
    hud.display_class(Panel)().send_h264_chunk(b'frame')
  assert stopped.value.code == 0 and not (tmp_path / 'hud-status.json').exists()


def test_optional_display_snapshot_retries_after_three_crashes_without_busy_loop(monkeypatch, tmp_path):
  from openpilot.selfdrive.modeld.jetlink import display as producer

  class DeadProcess:
    def poll(self):
      return 1

  class LiveProcess:
    def poll(self):
      return None

  launched = []
  monkeypatch.setattr(producer.subprocess, 'Popen', lambda *a, **kw: launched.append(a) or DeadProcess())
  p = producer.DisplayPublisher.__new__(producer.DisplayPublisher)
  p.path = tmp_path / 'display.packet'
  p.process = DeadProcess()
  p.worker_attempts = 3
  p.worker_retry_at = 100.
  p.worker_healthy_since = None
  p.last_sent = 17.
  p.status = {'hud_produced_mono': 0.}

  p.recover_snapshot(99.9)
  assert not launched
  p.recover_snapshot(100.)
  assert len(launched) == 1 and p.worker_attempts == 4
  assert p.worker_retry_at == 116.
  assert p.last_sent == 0.

  # Persistent errors are throttled instead of shutting HUD recovery off.
  p.recover_snapshot(115.)
  assert len(launched) == 1
  p.recover_snapshot(116.)
  assert len(launched) == 2 and p.worker_attempts == 5
  p.recover_snapshot(148.)
  p.recover_snapshot(212.)
  assert p.worker_attempts == 7
  assert p.worker_retry_at == 272.  # capped at one launch per 60 s

  # Recovery stays local to the optional snapshot worker. When its data is
  # continuously fresh for 30 seconds the crash budget resets.
  p.process = LiveProcess()
  p.status['hud_produced_mono'] = 300.
  p.recover_snapshot(300.)
  p.status['hud_produced_mono'] = 330.
  p.recover_snapshot(330.)
  assert p.worker_attempts == 0

def test_new_receiver_snapshot_cannot_renew_older_scene(monkeypatch, tmp_path):
  current = {'updated': 100., 'session': 'new'}
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: current)
  monkeypatch.setattr(hud.time, 'monotonic', lambda: 100.)
  @contextmanager
  def capture():
    yield b'vehicle', 32, 24
  for consumed in ({'updated': 99.49, 'session': 'new'}, {'updated': 99.99, 'session': 'old'}, None):
    with hud.guarded_capture(capture, lambda: consumed)() as (pixels, w, h):
      assert pixels == hud.waiting_frame(w, h, False)


def test_external_hud_auto_zero_preserves_lit_level_and_manual_setting(monkeypatch, tmp_path):
  from openpilot.tools.jetson import panel_events
  monkeypatch.setattr(panel_events, 'RUNTIME', tmp_path)
  brightness = hud.hud_brightness(lambda setting, source, **kw: setting or (80 if source is None else source))
  assert brightness(0, None, auto_enabled=True) == 80
  assert brightness(0, 24, auto_enabled=True) == 24
  assert brightness(0, 0, auto_enabled=True) == 24
  assert brightness(0, 0, auto_enabled=True) == 24
  assert brightness(0, 0, auto_enabled=False) == 0
  assert brightness(0, 36, auto_enabled=True) == 36
  assert [e['kind'] for e in panel_events.history('vehicle')] == ['auto_brightness_zero_held']
