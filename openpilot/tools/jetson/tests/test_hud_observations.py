from types import SimpleNamespace as NS

import pytest

from openpilot.tools.jetson import hud_observations as obs, hud, ncm_display


def signal(stamp=100, color='red', raw='red'):
  return {'version': 1, 'stream': 'road', 'timestamp': stamp, 'size': [1344, 760],
          'frame_id': 1, 'session': 'test',
          'tracks': [{'age': 0, 'box': [100, 100, 200, 200], 'state': color, 'evidence': {'raw': raw}}]}


@pytest.mark.parametrize('color,raw,expected', [('red', 'red', 'red'), ('green', 'green', 'green'),
                                               ('red', 'green', 'unknown'), ('unknown', None, 'unknown')])
def test_only_observed_signal_state_and_image_position(color, raw, expected):
  value = obs.signals(signal(color=color, raw=raw), 100.1)
  assert value == [{'kind': expected, 'x': 300 / 2688, 'y': 300 / 1520}]


@pytest.mark.parametrize('stamp', [99.4, 101, float('nan'), True, None])
def test_stale_or_invalid_signals_disappear(stamp):
  assert obs.signals(signal(stamp), 100) == []


def test_remote_signal_ages_on_receiver_and_never_invents_objects():
  value = {'updated': 10, 'signal_display': {'age_s': .2, 'items': obs.signals(signal(), 100)}}
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
