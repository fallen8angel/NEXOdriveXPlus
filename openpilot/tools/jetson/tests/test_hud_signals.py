"""Display observations use real producer format; fixtures are not optical validation."""
import json
from types import SimpleNamespace as NS

import pytest

from openpilot.tools.jetson import camera, hud_observations as obs
from openpilot.tools.jetson.hud_signals import MAX_WIRE, RemoteSignals, sanitize
from openpilot.tools.jetson.snapshot import validate_snapshot
from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet, HUD_LIMIT


def packet(stamp=100., sent=100.1, received=10., fid=1, session='observer', raw='red'):
  return {'version': 1, 'sent': sent, 'updated': received, 'session': 'a' * 32,
          'events': {}, 'params': {'IsOnroad': 'MQ==', 'IsMetric': 'MQ=='},
          'mono': {}, 'received': {}, 'valid': {}, 'alive': {},
          'cameras': {'road': {'frame': fid, 'time': stamp, 'source_width': 1344,
                               'source_height': 760, 'jpeg': 'fixture'}},
          'signal_display': {'version': 2, 'record': {
            'version': 1, 'stream': 'road', 'timestamp': stamp, 'size': [1344, 760],
            'frame_id': fid, 'session': session,
            'tracks': [{'id': 1, 'age': 0, 'box': [100, 100, 200, 200], 'state': 'red', 'evidence': {'raw': raw}}]}}}


@pytest.mark.parametrize('raw,expected', [('red', 'red'), ('green', 'green'), ('unknown', 'unknown'),
                                         ('yellow', 'unknown'), (None, 'unknown'), ({}, 'unknown')])
def test_only_current_optical_color(raw, expected):
  assert RemoteSignals().read(packet(raw=raw), 10.1)[0]['kind'] == expected


def test_red_green_unknown_and_no_detection_never_hold_old_color():
  reader = RemoteSignals()
  for index, color in enumerate(('red', 'green', 'unknown')):
    value = packet(stamp=100 + index, sent=100.1 + index, received=10 + index, fid=index, raw=color)
    assert reader.read(value, 10 + index)[0]['kind'] == color
  value = packet(stamp=103, sent=103.1, received=13, fid=3)
  value['signal_display']['record']['tracks'] = []
  assert reader.read(value, 13) == []
  assert reader.read(packet(), 13) == []


@pytest.mark.parametrize('field,bad', [('timestamp', float('nan')), ('timestamp', 101), ('timestamp', True),
                                     ('frame_id', -1), ('frame_id', True), ('session', ''), ('session', 's' * 129),
                                     ('tracks', {}), ('tracks', [None] * 21), ('size', [1920, 1080])])
def test_optional_error_does_not_invalidate_speed_or_navigation(field, bad):
  value = packet()
  value['signal_display']['record'][field] = bad
  value['events']['navInstruction'] = 'nav-data'
  result = validate_snapshot(value)
  assert result['signal_display']['record'] is None
  assert result['params']['IsOnroad'] == 'MQ==' and result['events']['navInstruction'] == 'nav-data'
  json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('change', ['frame', 'time', 'size', 'missing', 'empty_jpeg'])
def test_camera_mismatch_never_draws(change):
  value = packet()
  cam = value['cameras']['road']
  if change == 'frame':
    cam['frame'] += 1
  elif change == 'time':
    cam['time'] -= .01
  elif change == 'size':
    cam['source_width'] = 1920
  elif change == 'missing':
    value['cameras'].clear()
  else:
    cam['jpeg'] = ''
  assert RemoteSignals().read(value, 10) == []


def test_duplicate_timestamp_cannot_renew_receiver_deadline():
  reader = RemoteSignals()
  value = packet()
  assert reader.read(value, 10)
  value['updated'] = 10.2  # The identical sender packet is retransmitted.
  assert reader.read(value, 10.2)
  value['updated'] = 10.3
  assert reader.read(value, 10.3) == []
  # Even after a missing snapshot, that same detection must not reappear.
  assert reader.read(None, 10.31) == []
  assert reader.read(value, 10.32) == []


def test_changed_color_requires_a_new_observation_identity():
  reader = RemoteSignals()
  value = packet()
  assert reader.read(value, 10)[0]['kind'] == 'red'
  value['signal_display']['record']['tracks'][0]['evidence']['raw'] = 'green'
  assert reader.read(value, 10.1) == []
  assert reader.read(packet(stamp=100.2, sent=100.3, received=10.2, fid=2, raw='green'), 10.2)[0]['kind'] == 'green'


def test_old_frame_camera_restart_and_process_restart():
  reader = RemoteSignals()
  assert reader.read(packet(fid=40), 10)
  assert reader.read(packet(fid=39, stamp=100.1, sent=100.2, received=10.1), 10.1) == []
  restarted = packet(fid=1, session='new-camera', stamp=100.2, sent=100.3, received=10.2)
  assert reader.read(restarted, 10.2)
  assert reader.read(packet(fid=41), 10.2) == []  # previous producer session
  restarted = packet(fid=2, session='new-camera', stamp=100.3, sent=100.4, received=10.3)
  restarted['session'] = 'b' * 32
  assert reader.read(restarted, 10.3)
  assert RemoteSignals().read(restarted, 10.3)  # renderer restart
  assert RemoteSignals().read(restarted, 11) == []


def test_expired_and_legacy_identityless_data_hidden():
  value = packet()
  for bad in (None, [], {'age_s': 0, 'items': [{'kind': 'red', 'x': .5, 'y': .5}]}):
    value['signal_display'] = bad
    assert RemoteSignals().read(value, 10) == []
  assert RemoteSignals().read(packet(sent=100.4), 10) == []
  assert RemoteSignals().read(packet(received=11), 10) == []


def test_real_observation_writer_and_bounded_reader(tmp_path, monkeypatch):
  from openpilot.selfdrive.carrot.signal_assist_runtime import publish_observation
  path = tmp_path / 'observation.json'
  monkeypatch.setattr(obs, 'SIGNAL_PATH', path)
  value = packet()
  tracks = value['signal_display']['record']['tracks']
  tracks[0]['observations'] = 3
  publish_observation({'tracks': tracks}, 1, 100., 'producer', path)
  result = obs.signal_snapshot(100.1, value['cameras'])
  assert result['record']['session'] == 'producer' and result['record']['tracks'][0]['raw'] == 'red'
  for raw in (b'{', b'null', b'[]', b'{"timestamp": NaN}', b' ' * 32769):
    path.write_bytes(raw)
    assert obs.signal_snapshot(100.1, value['cameras'])['record'] is None
  path.unlink()
  assert obs.signal_snapshot(100.1, value['cameras'])['record'] is None


def test_large_signal_data_yields_to_core_hud_budget():
  value = packet()
  value['signal_display']['record']['untrusted'] = 'x' * 100000
  clean = sanitize(value['signal_display'], value['sent'], value['cameras'])
  assert len(json.dumps(clean).encode()) < MAX_WIRE
  core = dict(value)
  core.pop('signal_display')
  core['events'] = {'carState': ''}
  overhead = len(json.dumps(core, separators=(',', ':')).encode())
  value['events']['carState'] = 'c' * (HUD_LIMIT - overhead)
  raw = snapshot_packet(json.dumps(value).encode(), 'a' * 32)
  result = json.loads(raw)
  assert len(raw) <= HUD_LIMIT and result['events'] == value['events']
  assert result['cameras'] == value['cameras'] and 'signal_display' not in result


def test_road_preview_buffer_identity_checked_before_and_after_conversion(monkeypatch):
  frame = NS(frame_id=1, width=1344, height=760)
  client = NS(frame_id=1, timestamp_eof=100_000_000_000)
  monkeypatch.setattr(camera, 'jpeg_preview', lambda *args: (384, 216, b'image'))
  preview = camera.sample_preview(frame, client, now=100.1, road_identity=True)
  assert preview['source_width'] == 1344 and preview['frame'] == 1
  frame.frame_id = 2
  assert camera.sample_preview(frame, client, now=100.1, road_identity=True) is None
  frame.frame_id = 1
  def reused(*args):
    frame.frame_id = 2
    return 384, 216, b'image'
  monkeypatch.setattr(camera, 'jpeg_preview', reused)
  assert camera.sample_preview(frame, client, now=100.1, road_identity=True) is None


@pytest.mark.parametrize('mode', ['icon', 'inset'])
def test_overlay_failure_does_not_hide_other_objects_or_base_hud(monkeypatch, mode):
  from openpilot.tools.jetson import snapshot, yolo
  calls = []
  class Drawing:
    RED = GREEN = GRAY = WHITE = BLACK = 'color'
    def __getattr__(self, name):
      return lambda *args: calls.append((name, args))
  cls = type('Renderer', (), {'_render_world': lambda *a: calls.append(('base', ())), 'width': 1000, 'height': 500})
  monkeypatch.setenv('NEXO_HUD_SIGNALS', '1')
  monkeypatch.setenv('NEXO_HUD_OBJECTS', '1')
  monkeypatch.setenv('NEXO_HUD_SIGNAL_VIEW', mode)
  monkeypatch.setattr(yolo, 'read_objects', lambda: (_ for _ in ()).throw(ValueError('bad optional objects')))
  current = [packet(received=obs.time.monotonic())]
  monkeypatch.setattr(snapshot, 'read_snapshot', lambda: current[0])
  obs.install(NS(ClusterUiRenderer=cls, rl=Drawing()))
  renderer = cls()
  renderer._render_world(NS(onroad=True))
  assert calls[0][0] == 'base' and any(name == 'draw_circle' for name, _ in calls)
  calls.clear()
  current[0] = None
  renderer._render_world(NS(onroad=True))
  assert calls == [('base', ())]


def test_transport_reconnect_cannot_revive_identical_detection():
  reader = RemoteSignals()
  value = packet()
  assert reader.read(value, 10)
  value['session'] = 'b' * 32
  value['updated'] = 10.3
  assert reader.read(value, 10.3) == []
  fresh = packet(stamp=101, sent=101.1, received=11, session='reboot', fid=1)
  fresh['session'] = 'b' * 32
  assert reader.read(fresh, 11)
  fresh['session'] = 'a' * 32
  assert reader.read(fresh, 11) == []


@pytest.mark.parametrize('color,expected', [((255, 0, 0), 'red'), ((0, 255, 0), 'green'), (None, None)])
def test_synthetic_pixels_to_existing_detector_writer_and_hud(tmp_path, monkeypatch, color, expected):
  import cv2
  import numpy as np
  from tools.signal_analysis.signal_tracker import SignalTracker
  from openpilot.selfdrive.carrot.signal_assist_runtime import publish_observation
  rgb = np.zeros((760, 1344, 3), dtype=np.uint8)
  if color is not None:
    cv2.circle(rgb, (650, 260), 9, color, -1)
    cv2.circle(rgb, (650, 260), 3, (255, 255, 255), -1)
  detected = SignalTracker().process(rgb, 100.)
  path = tmp_path / 'observation.json'
  publish_observation(detected, 1, 100., 'synthetic-optical', path)
  monkeypatch.setattr(obs, 'SIGNAL_PATH', path)
  value = packet()
  value['signal_display'] = obs.signal_snapshot(100.1, value['cameras'])
  wire = snapshot_packet(json.dumps(value).encode(), 'a' * 32)
  lights = RemoteSignals().read(json.loads(wire), 10.)
  assert {light['kind'] for light in lights} == ({expected} if expected else set())
