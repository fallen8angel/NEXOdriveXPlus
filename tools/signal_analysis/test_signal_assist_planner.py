from types import SimpleNamespace as NS

import pytest

from tools.signal_analysis.replay_signal_assist import planner_class, REPO, ReplaySM  # noqa: TID251 - repository-root offline replay
from openpilot.selfdrive.carrot.signal_assist import SignalAssist


def inputs(speed=0.0, distance=100.0, lead=False):
  return ReplaySM(
    carState=NS(
      vCluRatio=1.0,
      softHoldActive=0,
      vEgo=speed,
      aEgo=0.0,
      vEgoCluster=speed,
      gasPressed=False,
      brakePressed=False,
      steeringAngleDeg=0.0,
      leftBlinker=False,
      rightBlinker=False,
    ),
    selfdriveState=NS(personality=1),
    radarState=NS(leadOne=NS(status=lead, dRel=10.0)),
    modelV2=NS(position=NS(x=[distance * i / 32 for i in range(33)], y=[0.0] * 33), velocity=NS(x=[speed] + [15.0] * 32), leadsV3=[]),
  )


def run(p, sm, state, start=0.0, count=20):
  for i in range(count):
    t = start + i * 0.05
    obs = dict(
      timestamp=t,
      frame_id=round(t * 20),
      session='same',
      tracks=[dict(id=1, box=[640, 250, 680, 270], state=state, age=0.0, observations=10, evidence={'raw': state})],
    )
    p.update(sm, 80.0, 'acc', signal_observation=obs, signal_context=dict(now=t, enabled=True, valid=True, drive=True))


def planner():
  cls, _ = planner_class((REPO / 'openpilot/selfdrive/carrot/carrot_functions.py').read_text(encoding='utf-8'))
  return cls(SignalAssist())


def test_red_hold_wins_over_model_go_and_lead_handover():
  p = planner()
  sm = inputs(lead=True)
  run(p, sm, 'red')
  assert p.xState.name == 'e2eStopped' and p.stop_dist == 0 and p.v_cruise == 0
  run(p, sm, 'unknown', 1.0)
  assert p.xState.name == 'e2eStopped'


def test_moving_red_enters_existing_stop_and_speed_cap():
  p = planner()
  sm = inputs(speed=10.0, distance=40.0)
  run(p, sm, 'red')
  assert p.xState.name == 'e2eStop'
  assert 0 < p.stop_dist < 40.0 and p.v_cruise < 80 / 3.6
  assert 0 < p.comfort_brake <= 2.4


def test_green_does_not_force_departure_against_original_model():
  p = planner()
  sm = inputs(distance=3.0)
  sm['modelV2'].velocity.x = [0.0] * 33
  run(p, sm, 'red')
  run(p, sm, 'green', 1.0)
  assert not p.signal_decision.hold
  assert p.xState.name == 'e2eStopped' and p.v_cruise == 0


def test_driver_gas_releases_hold_through_original_override():
  p = planner()
  sm = inputs()
  run(p, sm, 'red')
  sm['carState'].gasPressed = True
  run(p, sm, 'red', 1.0, 1)
  assert not p.signal_decision.hold and p.xState.name == 'e2eCruise'


@pytest.mark.parametrize('gate', ['high_mode', 'detect_off', 'lead', 'steering', 'turning', 'cooldown'])
def test_nexo_moving_entry_gates_are_retained(gate):
  p = planner()
  sm = inputs(speed=10.0, distance=40.0)
  if gate == 'high_mode':
    p.myDrivingMode = type(p.myDrivingMode).High
  elif gate == 'detect_off':
    p.trafficLightDetectMode = 0
  elif gate == 'lead':
    sm['radarState'].leadOne.status = True
  elif gate == 'steering':
    sm['carState'].steeringAngleDeg = 21.0
  elif gate == 'turning':
    sm['carState'].leftBlinker = True
  elif gate == 'cooldown':
    p.traffic_starting_count = 100
  run(p, sm, 'red')
  assert not p.signal_decision.red_sign and not p.signal_decision.hold


@pytest.mark.parametrize('speed', [0.0, 3.0, 10.0, 25.0])
@pytest.mark.parametrize('lead', [False, True])
def test_disabled_assist_keeps_nexo_outputs_and_never_reads_observation(monkeypatch, speed, lead):
  from openpilot.selfdrive.carrot import signal_assist_runtime

  monkeypatch.setattr(signal_assist_runtime, 'requested', lambda *args: False)
  cls, _ = planner_class((REPO / 'openpilot/selfdrive/carrot/carrot_functions.py').read_text(encoding='utf-8'))
  normal, disabled = cls(), cls(SignalAssist())
  assert normal.signal_assist is None and normal._signal_runtime is None

  def unexpected_read(*args):
    raise AssertionError('OFF path must not read observer records')

  monkeypatch.setattr(signal_assist_runtime.SignalAssistRuntime, 'read', unexpected_read)
  sm = inputs(speed=speed, distance=40.0, lead=lead)
  for _ in range(40):
    normal.update(sm, 80.0, 'acc')
    disabled.update(sm, 80.0, 'acc', signal_context=dict(enabled=False))
    for key in ('xState', 'trafficState', 'v_cruise', 'stop_dist', 'actual_stop_distance', 'fakeCruiseDistance', 'comfort_brake'):
      assert getattr(normal, key) == getattr(disabled, key)
