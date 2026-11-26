from contextlib import contextmanager
from functools import partial
from types import SimpleNamespace as NS

from PIL import Image
import pytest

from openpilot.tools.jetson import hud_ignition as ignition
from openpilot.tools.jetson.startup_animation import StartupAnimation, claim, decode, eligible


def snapshot(cycle='a' * 32, **kw):
  return {'hud_startup': dict(on=True, cycle=cycle, age=3., safe=True, **kw)}


def player(tmp_path, now, **kw):
  p = StartupAnimation(claim_cycle=partial(claim, path=tmp_path / 'claims.db'), clock=lambda: now[0], **kw)
  try:
    p.future.result(timeout=5)
  except Exception:
    pass
  return p


FRAME = (Image.new('RGBA', (320, 80), '#263b44').tobytes(), 320, 80)


def test_real_gif_preserves_nine_frames_and_original_timing():
  frames, ends = decode()
  assert len(frames) == 9 and frames[0].size == (553, 309)
  assert ends[-1] == pytest.approx(2.07)


def test_observed_off_on_only_and_persistent_service_restarts(tmp_path):
  path = tmp_path / 'ignition.json'
  c = ignition.IgnitionCycles(path, 'boot1')
  assert c.observe(True, 1)['cycle'] is None  # startup is not ignition proof
  assert c.observe(False, 2) == {'on': False}
  c.observe(False, 3)
  first = c.observe(True, 4)['cycle']
  assert first
  assert ignition.IgnitionCycles(path, 'boot1').observe(True, 8)['cycle'] == first
  assert c.observe(None, 8) is None
  assert c.observe(True, 9)['cycle'] == first
  assert ignition.IgnitionCycles(path, 'boot2').observe(True, 2)['cycle'] is None
  c.observe(False, 10)
  c.observe(False, 11)
  assert c.observe(True, 12)['cycle'] != first


def test_unknown_gaps_and_short_off_do_not_rearm(tmp_path):
  c = ignition.IgnitionCycles(tmp_path / 'state', 'boot')
  c.observe(False, 0)
  c.observe(None, .5)
  c.observe(False, 2)
  assert c.observe(True, 2.5)['cycle'] is None
  c.observe(False, 3)
  c.observe(False, 4)
  token = c.observe(True, 5)['cycle']
  c.observe(False, 6)
  assert c.observe(True, 6.1)['cycle'] == token
  c.inhibit()  # driving/reverse/neutral permanently consumes this opportunity
  assert c.observe(True, 7)['cycle'] is None
  assert ignition.IgnitionCycles(c.path, 'boot').observe(True, 8)['cycle'] is None


def test_repeated_single_off_sample_does_not_rearm(tmp_path):
  c = ignition.IgnitionCycles(tmp_path / 'state', 'boot')
  c.observe(True, 1)
  for _ in range(20):
    c.observe(False, 2)
  assert c.observe(True, 3)['cycle'] is None


def test_corrupt_source_and_persistence_failure_fail_closed(tmp_path, monkeypatch):
  path = tmp_path / 'state'
  path.write_text('{broken')
  c = ignition.IgnitionCycles(path, 'boot')
  assert c.observe(True, 0)['cycle'] is None
  c.observe(False, 1)
  c.observe(False, 2)
  monkeypatch.setattr(ignition, 'durable_json', lambda *args: (_ for _ in ()).throw(OSError()))
  with pytest.raises(OSError):
    c.observe(True, 3)
  assert c.state == {'on': False}


def test_restart_usb_recovery_does_not_replay_and_new_cycle_does(tmp_path):
  now, s = [0.], snapshot()
  first = player(tmp_path, now)
  assert first.compose(FRAME, s, s) != FRAME
  first.close()  # crash, HUD restart, USB loss or service restart
  for _ in range(3):
    next_player = player(tmp_path, now)
    assert next_player.compose(FRAME, s, s) == FRAME
    next_player.close()
  next_player = player(tmp_path, now)
  s = snapshot('b' * 32)
  assert next_player.compose(FRAME, s, s) != FRAME
  next_player.close()


@pytest.mark.parametrize('cause', ['reverse', 'warning', 'gap', 'off', 'motion', 'neutral', 'unknown'])
def test_priority_interrupt_is_immediate_and_permanent(tmp_path, cause):
  now, s = [0.], snapshot()
  p = player(tmp_path, now)
  assert p.compose(FRAME, s, s) != FRAME
  current = snapshot()
  if cause == 'gap':
    current = None
  elif cause == 'off':
    current['hud_startup']['on'] = False
  else:
    current['hud_startup']['safe'] = False
  assert p.compose(FRAME, s, current) == FRAME
  assert p.compose(FRAME, s, s) == FRAME
  p.close()


def test_live_park_crossfade_no_black_frame_and_no_loop(tmp_path):
  now, s = [0.], snapshot()
  p = player(tmp_path, now)
  p.compose(FRAME, s, s)
  now[0] = 2.07
  last = p.compose(FRAME, s, s)[0]
  now[0] = 2.22
  middle = p.compose(FRAME, s, s)[0]
  assert middle != last and middle != FRAME[0]
  assert max(middle) > 0
  now[0] = 2.38
  assert p.compose(FRAME, s, s) == FRAME
  now[0] = 3.
  assert p.compose(FRAME, s, s) == FRAME
  p.close()


def test_portrait_mirror_matches_existing_transform(tmp_path):
  now, s = [0.], snapshot()
  p = player(tmp_path, now)
  normal = Image.frombytes('RGBA', (320, 80), p.compose(FRAME, s, s)[0])
  rotated = p.compose((bytes(320*80*4), 80, 320), s, s, portrait=True, flip_x=True)[0]
  assert rotated == normal.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.ROTATE_270).tobytes()
  p.close()


@pytest.mark.parametrize('bad', [b'broken', b'', b'GIF89a'])
def test_broken_asset_falls_through_to_normal_hud(tmp_path, bad):
  path = tmp_path / 'bad.gif'
  path.write_bytes(bad)
  p = player(tmp_path, [0.], asset=path)
  assert p.compose(FRAME, snapshot(), snapshot()) == FRAME
  p.close()


def test_claim_failure_and_late_join_skip_without_blocking_hud(tmp_path):
  s = snapshot()
  p = player(tmp_path, [0.])
  p.claim_cycle = lambda cycle: (_ for _ in ()).throw(OSError())
  assert p.compose(FRAME, s, s) == FRAME
  p.close()
  s['hud_startup']['age'] = 91.
  assert eligible(s) is None


def test_panda_unknown_empty_stale_is_not_off():
  class SM(dict):
    valid = {'pandaStates': True}
    alive = {'pandaStates': True}
    logMonoTime = {'pandaStates': 10_000_000_000}
  sm = SM(pandaStates=[])
  assert ignition.panda_ignition(sm, 10) is None
  sm['pandaStates'] = [NS(pandaType='unknown', ignitionLine=False, ignitionCan=False)]
  assert ignition.panda_ignition(sm, 10) is None
  sm['pandaStates'] = [NS(pandaType='tres', ignitionLine=True, ignitionCan=False)]
  assert ignition.panda_ignition(sm, 10) is True
  assert ignition.panda_ignition(sm, 12) is None


def test_metadata_safety_is_derived_from_actual_fresh_vehicle_messages(monkeypatch):
  class SM(dict):
    valid = dict(carState=True, selfdriveState=True)
    alive = valid.copy()
    logMonoTime = dict(carState=10_000_000_000, selfdriveState=10_000_000_000)
  sm = SM(carState=NS(canValid=True, canTimeout=False, standstill=True, vEgo=0., gearShifter='park',
                     leftBlinker=False, rightBlinker=False),
          selfdriveState=NS(alertText1='', alertText2='', alertSize='none', alertStatus='normal'))
  monkeypatch.setattr(ignition, 'read_fresh', lambda *args: dict(on=True, cycle='a'*32, age=2.))
  read = lambda: ignition.startup_metadata(sm, 10, nexo=True, park_enabled=True)
  assert read()['safe']
  for gear in ('drive', 'neutral', 'reverse', 'unknown'):
    sm['carState'].gearShifter = gear
    assert not read()['safe']
  sm['carState'].gearShifter = 'park'
  sm['selfdriveState'].alertText1 = 'collision warning'
  assert not read()['safe']
  sm['selfdriveState'].alertText1 = ''
  sm.logMonoTime['selfdriveState'] = 9_000_000_000
  assert not read()['safe']


def test_guard_cancels_on_stale_input(monkeypatch, tmp_path):
  from openpilot.tools.jetson import hud
  p = player(tmp_path, [0.])
  s = snapshot()
  p.compose(FRAME, s, s)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  @contextmanager
  def capture():
    yield FRAME
  with hud.guarded_capture(capture, startup=p)() as result:
    assert result[1:] == FRAME[1:]
  assert p.cycle is None
  assert p.compose(FRAME, s, s) == FRAME
  p.close()
