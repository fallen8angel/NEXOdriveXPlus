import json
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot.signal_assist_runtime import SignalAssistRuntime, publish_observation


class SM(dict):
  def __init__(self):
    super().__init__(carState=NS(canValid=True, gearShifter='drive'), selfdriveState=NS(enabled=True), carControl=NS(longActive=True))
    names = ['carState', 'modelV2', 'radarState', 'selfdriveState', 'carControl']
    self.seen = dict.fromkeys(names, True)
    self.valid = dict.fromkeys(names, True)
    self.alive = dict.fromkeys(names, True)
    self.recv_time = dict.fromkeys(names, 10.0)


def test_default_does_not_enable_control(tmp_path):
  runtime = SignalAssistRuntime(tmp_path / 'enable', tmp_path / 'obs')
  assert runtime.assist is None


@pytest.mark.parametrize('value', [b'\xff', b'yes', b'1' + b' ' * 16 + b'0', b'', b'0'])
def test_malformed_enable_flags_fail_closed(tmp_path, value):
  from openpilot.selfdrive.modeld.signal_color_shadow import requested as observer_requested
  from openpilot.selfdrive.modeld.signal_tracking_shadow import tracking_requested

  for name in ('assist_enabled', 'enabled', 'tracking_enabled'):
    (tmp_path / name).write_bytes(value)
  assert SignalAssistRuntime(tmp_path / 'assist_enabled', tmp_path / 'obs').assist is None
  assert not observer_requested(tmp_path) and not tracking_requested(tmp_path)


def test_atomic_round_trip_and_disable(tmp_path):
  flag, path = tmp_path / 'enable', tmp_path / 'obs'
  flag.write_text('1')
  runtime = SignalAssistRuntime(flag, path)
  publish_observation({'tracks': []}, 1, 10.0, 'worker', path)
  obs, context = runtime.read(SM(), 10.1)
  assert obs['timestamp'] == 10.0 and context['enabled'] and context['valid']
  flag.write_text('0')
  obs, context = runtime.read(SM(), 10.7)
  assert obs is None and not context['enabled']


@pytest.mark.parametrize(
  'payload',
  ['invalid', '[]', '{}', 'x' * 40000, '[' * 1500 + ']' * 1500, json.dumps(dict(version=2, stream='road', size=[1344, 760]))],
  ids=['bad_json', 'list', 'missing', 'oversized', 'deep_json', 'wrong_version'],
)
def test_bad_transport_is_unknown(tmp_path, payload):
  flag, path = tmp_path / 'enable', tmp_path / 'obs'
  flag.write_text('1')
  path.write_text(payload)
  obs, _ = SignalAssistRuntime(flag, path).read(SM(), 10.1)
  assert obs is None


@pytest.mark.parametrize('tracks', [None, 'wrong', [{}] * 21])
def test_observation_track_count_is_bounded(tmp_path, tracks):
  flag, path = tmp_path / 'enable', tmp_path / 'obs'
  flag.write_text('1')
  path.write_text(json.dumps(dict(version=1, stream='road', size=[1344, 760], tracks=tracks)))
  obs, _ = SignalAssistRuntime(flag, path).read(SM(), 10.1)
  assert obs is None


@pytest.mark.parametrize('condition', ['stale', 'unseen', 'invalid', 'dead', 'park', 'inactive'])
def test_runtime_gates_vehicle_context(tmp_path, condition):
  flag = tmp_path / 'enable'
  flag.write_text('1')
  sm = SM()
  if condition == 'stale':
    sm.recv_time['carState'] = 9.0
  elif condition == 'unseen':
    sm.seen['modelV2'] = False
  elif condition == 'invalid':
    sm.valid['carState'] = False
  elif condition == 'dead':
    sm.alive['radarState'] = False
  elif condition == 'park':
    sm['carState'].gearShifter = 'park'
  elif condition == 'inactive':
    sm['carControl'].longActive = False
  _, context = SignalAssistRuntime(flag, tmp_path / 'absent').read(sm, 10.1)
  assert not all(context[x] for x in ('valid', 'enabled', 'drive'))
