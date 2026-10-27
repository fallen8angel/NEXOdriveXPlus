from types import SimpleNamespace as NS

import pytest

from openpilot.tools.jetson import hud_observations as obs, hud, ncm_display


def signal(stamp=100, color='red', raw='red'):
  return {'version': 1, 'stream': 'road', 'timestamp': stamp, 'size': [1344, 760],
          'frame_id': 1, 'session': 'test',
          'tracks': [{'id': 1, 'age': 0, 'box': [100, 100, 200, 200], 'state': color, 'evidence': {'raw': raw}}]}


@pytest.mark.parametrize('color,raw,expected', [('red', 'red', 'red'), ('green', 'green', 'green'),
                                               ('red', 'green', 'green'), ('unknown', None, 'unknown')])
def test_only_observed_signal_state_and_image_position(color, raw, expected):
  value = obs.signals(signal(color=color, raw=raw), 100.1)
  assert value == [{'kind': expected, 'x': 300 / 2688, 'y': 300 / 1520}]


@pytest.mark.parametrize('stamp', [99.4, 101, float('nan'), True, None])
def test_stale_or_invalid_signals_disappear(stamp):
  assert obs.signals(signal(stamp), 100) == []


def test_remote_signal_ages_on_receiver_and_never_invents_objects():
  from openpilot.tools.jetson.tests.test_hud_signals import packet
  value = packet(stamp=100, sent=100.2, received=10)
  assert obs.remote_signals(value, 10.1)
  assert obs.remote_signals(value, 10.4) == []
  assert obs.observed_objects(None) == []
  objects = {'width': 100, 'height': 100, 'objects': [
    {'name': name, 'conf': .9, 'x1': 10, 'x2': 20, 'y1': 20, 'y2': 40}
    for name in ('car', 'person', 'motorcycle', 'tree')]}
  assert [v['kind'] for v in obs.observed_objects(objects)] == ['car', 'person', 'motorcycle']
  assert all(set(v) == {'kind', 'x', 'y'} for v in obs.observed_objects(objects))


def test_independent_default_off_and_overlay_failure_isolated(monkeypatch):
  calls = []
  cls = type('Renderer', (), {'_render_world': lambda *a: calls.append('base')})
  renderer = NS(ClusterUiRenderer=cls)
  monkeypatch.delenv('NEXO_HUD_OBJECTS', raising=False)
  monkeypatch.delenv('NEXO_HUD_SIGNALS', raising=False)
  original = cls._render_world
  obs.install(renderer)
  assert cls._render_world is original
  monkeypatch.setenv('NEXO_HUD_SIGNALS', '1')
  obs.install(renderer)
  cls()._render_world(NS(onroad=True))
  assert calls == ['base']


def test_ncm_diagnostic_enospc_cannot_disconnect_transport(monkeypatch):
  def full(*args):
    raise OSError(28, 'full')
  monkeypatch.setattr(ncm_display, 'atomic_json', full)
  ncm_display.publish('connected')
  ncm_display.publish('retrying', error='test')
  ncm_display.publish('connected')
  assert ncm_display._diagnostic_events[-2]['error'] == 'test'


def test_hud_diagnostic_enospc_does_not_restart_renderer(monkeypatch):
  from openpilot.tools.jetson.snapshot import encode
  monkeypatch.setattr(hud, 'read_snapshot', lambda: {'params': {'IsOnroad': encode(True)}})
  def full(*args):
    raise OSError(28, 'full')
  monkeypatch.setattr(hud, 'atomic_json', full)
  class Panel:
    def send_h264_chunk(self, raw):
      return len(raw)
  assert hud.display_class(Panel)().send_h264_chunk(b'frame') == 5


def test_predicted_signal_track_is_not_displayed():
  value = signal()
  value['tracks'][0]['age'] = .01
  assert obs.signals(value, 100) == []


def test_both_overlays_render_observed_icons_and_drop_expired_data(monkeypatch):
  from openpilot.tools.jetson import yolo, snapshot
  calls = []
  class Drawing:
    WHITE = RED = GREEN = GRAY = BLACK = 'color'
    def __getattr__(self, name):
      return lambda *args: calls.append(name)
  cls = type('Renderer', (), {'_render_world': lambda *a: calls.append('base'), 'width': 1000, 'height': 500})
  monkeypatch.setenv('NEXO_HUD_OBJECTS', '1')
  monkeypatch.setenv('NEXO_HUD_SIGNALS', '1')
  objects = {'width': 100, 'height': 100, 'objects': [
    {'name': k, 'conf': .9, 'x1': 10, 'x2': 20, 'y1': 20, 'y2': 30} for k in ('car', 'person', 'motorcycle')]}
  fresh = [True]
  monkeypatch.setattr(yolo, 'read_objects', lambda: objects if fresh[0] else None)
  from openpilot.tools.jetson.tests.test_hud_signals import packet
  monkeypatch.setattr(snapshot, 'read_snapshot', lambda: packet(received=obs.time.monotonic()) if fresh[0] else None)
  obs.install(NS(ClusterUiRenderer=cls, rl=Drawing()))
  cls()._render_world(NS(onroad=True))
  assert calls[0] == 'base'
  assert all(v in calls for v in ('draw_rectangle_lines', 'draw_line', 'draw_circle_lines', 'draw_circle'))
  calls.clear()
  fresh[0] = False
  cls()._render_world(NS(onroad=True))
  assert calls == ['base']
