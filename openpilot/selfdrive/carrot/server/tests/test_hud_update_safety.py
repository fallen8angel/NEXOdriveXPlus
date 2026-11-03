"""Display-only HUD update parking gate: no vehicle writes or live hardware."""
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot.server.services import hud_update_safety as safety


def vehicle(gear='park', speed=0., standstill=True, gas=False, lateral=False, longitudinal=False):
  return (NS(gearShifter=gear, vEgo=speed, standstill=standstill, gasPressed=gas),
          NS(enabled=lateral), NS(enabled=longitudinal))


def test_park_proof_accepts_only_P_stationary_and_disengaged():
  assert safety._evaluate_park(*vehicle()) is None


@pytest.mark.parametrize('variation,rejection', [
  ({'gear': 'drive'}, 'P단'),
  ({'gear': 'neutral'}, 'P단'),
  ({'speed': 0.2}, '정지'),
  ({'speed': float('nan')}, '정지'),
  ({'standstill': False}, '정지'),
  ({'gas': True}, '가속'),
  ({'lateral': True}, '주행 제어'),
  ({'longitudinal': True}, '주행 제어'),
])
def test_does_not_approve_unsafe_vehicle(variation, rejection):
  assert rejection in safety._evaluate_park(*vehicle(**variation))


class Params:
  def __init__(self, offroad, onroad):
    self.values = {'IsOffroad': offroad, 'IsOnroad': onroad}
  def get_bool(self, key):
    return self.values[key]


def test_offroad_keeps_existing_maintenance_path():
  approved, reason = safety.installation_ready(Params(True, False),
                                               park_probe=lambda: pytest.fail('no carState needed offroad'))
  assert approved and 'Offroad' in reason


def test_onroad_stationary_park_needs_real_can_proof():
  calls = []
  def probe():
    calls.append(True)
    return True, 'live park confirmed'
  approved, reason = safety.installation_ready(Params(False, True), park_probe=probe)
  assert approved and calls == [True] and reason == 'live park confirmed'


@pytest.mark.parametrize('offroad,onroad', [(False, False), (True, True)])
def test_ambiguous_param_states_fail_closed(offroad, onroad):
  approved, _ = safety.installation_ready(Params(offroad, onroad),
                                          park_probe=lambda: pytest.fail('must not trust ambiguous flags'))
  assert not approved


def test_onroad_vehicle_proof_failure_blocks_install():
  approved, reason = safety.installation_ready(Params(False, True), park_probe=lambda: (False, 'gear not in P'))
  assert not approved and 'gear not in P' in reason


def test_state_transition_after_proof_blocks_install():
  params = Params(False, True)
  def change_gear():
    params.values['IsOnroad'] = False
    return True, 'P'
  approved, _ = safety.installation_ready(params, park_probe=change_gear)
  assert not approved


def test_missing_live_bus_never_succeeds(monkeypatch):
  from openpilot.cereal import messaging
  class EmptyBus:
    def __init__(self, *a, **kw):
      self.seen = dict.fromkeys(safety.SERVICES, False)
      self.valid = dict.fromkeys(safety.SERVICES, False)
      self.logMonoTime = dict.fromkeys(safety.SERVICES, 0)
      self.recv_time = dict.fromkeys(safety.SERVICES, 0)
    def update(self, timeout):
      pass
  monkeypatch.setattr(messaging, 'SubMaster', EmptyBus)
  monkeypatch.setattr(safety, 'POLL_DEADLINE_S', 0.02)
  approved, reason = safety.live_park_proof()
  assert not approved and '데이터' in reason
