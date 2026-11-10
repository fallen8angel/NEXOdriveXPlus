"""Exercise the installed Jetson scene hook and production RGBA framebuffer."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cluster'))
import cluster_renderer
from cluster_models import SimulatorInput
from cluster_simulator import ClusterSimulator
from openpilot.tools.jetson.gear_view import GearSelector, GearView, park_summary, install
from openpilot.tools.jetson.rear_camera import WIDTH, HEIGHT


def packet(gear, tick, **kwargs):
  stamp = 100_000_000_000 + tick * 100_000_000
  return dict(session='one', sent=stamp/1e9, updated=time.monotonic(),
              mono={'carState': stamp}, valid={'carState': True}, alive={'carState': True},
              gear_view=dict(gear=gear, sample_ns=stamp, can_ok=True, park_enabled=True, **kwargs))


def test_gear_filter_rejects_replays_gaps_and_session_changes():
  selector = GearSelector()
  def apply(s):
    return selector.update(s, time.monotonic())[0]
  p = packet('park', 1)
  assert apply(p) is None and apply(p) is None
  assert apply(packet('drive', 2)) is None
  assert apply(packet('park', 3)) is None
  assert apply(packet('park', 4)) == 'park'
  assert apply(packet('reverse', 5)) == 'park'
  assert apply(packet('unknown', 6)) == 'park'
  p = packet('reverse', 7)
  p['gear_view']['can_ok'] = False
  assert apply(p) == 'park'
  p = packet('reverse', 8)
  p['updated'] -= .51
  assert apply(p) == 'park'
  p = packet('reverse', 8)
  p['gear_view']['sample_ns'] -= 700_000_000
  assert apply(p) == 'park'
  assert apply(packet('reverse', 9)) == 'park'
  assert apply(packet('reverse', 10)) == 'reverse'
  assert apply(dict(packet('park', 1), session='two')) is None


def test_gear_framebuffer_and_original_driving_renderer(monkeypatch):
  original = cluster_renderer.ClusterUiRenderer
  class Renderer(original):
    pass
  monkeypatch.setattr(cluster_renderer, 'ClusterUiRenderer', Renderer)
  source = {}
  install(cluster_renderer, lambda: source, lambda _: '12:30:00')
  ui = Renderer(width=960, height=240)
  ui.open(hidden=True)
  state = replace(ClusterSimulator().update(SimulatorInput(), .1), onroad=True)
  tick = 0
  def draw(gear, expected, *, enabled=True, left=False, right=False):
    nonlocal tick
    for _ in range(2):
      tick += 1
      source.update(packet(gear, tick, left=left, right=right))
      source['gear_view']['park_enabled'] = enabled
      rgba, w, h = ui.render_to_rgba_bytes(replace(state, gear_text=gear[0].upper()))
      frame = np.frombuffer(rgba, np.uint8).reshape(h, w, 4)
      assert np.count_nonzero(frame[:, :, :3].max(axis=2) > 50) > w*h*.003
    assert ui._jetson_gears.mode == expected
    return frame
  try:
    # A newly started Jetson renderer consumes the saved Comma preference.
    draw('park', 'drive', enabled=False)
    assert ui._jetson_gears.texture is None
    for _ in range(3):
      park = draw('park', 'park')
      texture = ui._jetson_gears.texture.id
      assert texture > 0
      drive = draw('drive', 'drive')
      assert not np.array_equal(park, drive)
      draw('park', 'drive', enabled=False)
      draw('park', 'park')
      assert ui._jetson_gears.texture.id == texture
      reverse = draw('reverse', 'reverse')
      # Provide a synthetic camera frame to the actual pixel upload path.
      rear = ui._jetson_gears.rear
      rear.stop.clear()  # A new capture worker has started for this R engagement.
      rear.latest = (time.monotonic(), bytes((15, 222, 31)) * WIDTH * HEIGHT)
      video = draw('reverse', 'reverse')
      assert (video[100, 380, :3] == (15, 222, 31)).all()
      rear.latest = (time.monotonic()-1, rear.latest[1])
      assert np.array_equal(draw('reverse', 'reverse'), reverse)
      draw('neutral', 'neutral')
      assert rear.texture is None and rear.retry_at == 0.
    # Freeze the clock after the entry animation. Check the actual amber
    # pixels of each lamp independently and their shared hazard phase.
    now = [200.]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    draw('park', 'park')
    now[0] += .8
    def amber(**signals):
      frame = draw('park', 'park', **signals)
      return (frame[:, :, :3] == (255, 176, 45)).all(axis=2)
    left, right = amber(left=True), amber(right=True)
    assert left.any() and right.any() and not (left & right).any()
    assert np.array_equal(amber(left=True, right=True), left | right)
    now[0] += .4
    assert not amber(left=True, right=True).any()
    for gear in ('drive', 'neutral'):
      actual = draw(gear, gear)
      with monkeypatch.context() as patch:
        patch.setattr(Renderer, 'render', original.render)
        rgba, _, _ = ui.render_to_rgba_bytes(replace(state, gear_text=gear[0].upper()))
      assert actual.tobytes() == rgba
  finally:
    ui.close()


def test_benchmarked_park_summary_uses_only_valid_vehicle_and_trip_values():
  trip = SimpleNamespace(duration_s=645., distance_m=4920., average_speed_kph=41.8,
                         auto_ratio_percent=92.)
  state = SimpleNamespace(energy_gauge_label='fuel', fuel_gauge=.72,
                          tpms=SimpleNamespace(fl=32., fr=33., rl=None, rr=31.),
                          trip_report=trip)
  info = park_summary(state, fresh=True)
  assert info['hydrogen'] == '72%'
  assert info['front'] == '32/33' and info['rear'] == '--/31'
  assert info['trip'] == (('거리', '4.92 km'), ('시간', '10분'), ('평균', '41.8 km/h'), ('자동주행', '92%'))
  assert park_summary(state, fresh=False)['trip'][0] == ('거리', '--')
  assert park_summary(state, fresh=False)['hydrogen'] == '--'
  assert park_summary(state, fresh=True, metric=False)['trip'][0] == ('거리', '3.06 mi')
  bad = SimpleNamespace(energy_gauge_label='battery', fuel_gauge=float('nan'),
                        tpms=SimpleNamespace(fl=-10., fr=150., rl=0., rr=None),
                        trip_report=SimpleNamespace(duration_s=-1., distance_m=float('inf'),
                                                    average_speed_kph=float('nan'), auto_ratio_percent=101.))
  info = park_summary(bad, fresh=True)
  assert info['hydrogen'] == '--' and info['front'] == '--/--'
  assert all(value == '--' for _, value in info['trip'])


def test_jetson_gear_view_falls_back_after_stale_evidence():
  view = GearView()
  assert view._selected_gear('park', False, 100.) is None
  assert view._selected_gear('park', True, 100.) == 'park'
  assert view._selected_gear('park', False, 100.7) == 'park'
  assert view._selected_gear('park', False, 101.01) is None
  assert view._selected_gear('reverse', True, 101.2) == 'reverse'
