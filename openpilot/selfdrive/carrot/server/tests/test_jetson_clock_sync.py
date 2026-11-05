"""No device contact: Comma -> Jetson clock correction with stationary-P fail closed."""
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot.server.services import jetson_clock_sync as clock


HOST = 'jetlink@192.168.100.250'
GOOD_TIME = 1793876000  # 2026-era trusted Comma epoch
OLD_TIME = 1756259200   # incorrect Jetson boot clock


def fake_run_for(values, commands):
  def fake_run(argv, **kwargs):
    commands.append(argv)
    assert argv[:2] == ['ssh', '-T']
    assert 'StrictHostKeyChecking=yes' in argv
    assert 'BatchMode=yes' in argv
    assert argv[-2] == HOST
    return NS(returncode=0, stdout=str(values.pop(0)) + '\n', stderr='')
  return fake_run


def test_corrects_stale_jetson_date_and_verifies_result():
  commands = []
  values = [OLD_TIME, GOOD_TIME]
  calls = []
  def safe():
    calls.append(True)
    return True, 'park'
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME,
                           run=fake_run_for(values, commands), ready=safe)
  assert result['ok'] and result['status'] == 'updated'
  assert result['old_drift_s'] == GOOD_TIME - OLD_TIME
  assert len(commands) == 2
  assert commands[0][-1] == 'date -u +%s'
  assert 'sudo -n date -u -s @' + str(GOOD_TIME) in commands[1][-1]
  assert calls == [True, True]


def test_keeps_good_time_untouched():
  commands = []
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME,
                           run=fake_run_for([GOOD_TIME - 2], commands),
                           ready=lambda: (True, 'park'))
  assert result['ok'] and result['status'] == 'in_sync'
  assert len(commands) == 1


def test_refuses_unsafe_or_unknown_vehicle_without_ssh():
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME,
                           run=lambda *a, **kw: pytest.fail('must not SSH'),
                           ready=lambda: (False, 'D단'))
  assert result['status'] == 'not_parked'


def test_refuses_clock_write_if_gear_changed_during_probe():
  commands = []
  checks = iter([(True, 'P'), (False, 'D')])
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME,
                           run=fake_run_for([OLD_TIME], commands),
                           ready=lambda: next(checks))
  assert result['status'] == 'park_changed'
  assert len(commands) == 1


def test_refuses_bootstrap_when_comma_time_untrusted():
  result = clock.sync_once(HOST, now=lambda: 1000,
                           run=lambda *a, **kw: pytest.fail('must not SSH'),
                           ready=lambda: pytest.fail('must not check gear'))
  assert result['status'] == 'comma_time_untrusted'


def test_requires_read_only_park_confirmation_by_default():
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME)
  assert result['status'] == 'park_proof_required'


@pytest.mark.parametrize('target', ['-evil', 'jetlink@1.2.3.4;sudo', '', None])
def test_rejects_invalid_target(target):
  result = clock.sync_once(target, now=lambda: GOOD_TIME, ready=lambda: (True, 'P'))
  assert result['status'] == 'invalid_target'


def test_rejects_failed_ssh_without_setting_time():
  commands = []
  def fail(argv, **kw):
    commands.append(argv)
    return NS(returncode=255, stdout='', stderr='Permission denied (publickey)')
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME, run=fail, ready=lambda: (True, 'P'))
  assert result['status'] == 'sync_failed'
  assert len(commands) == 1


def test_readback_must_match_comma_clock():
  commands = []
  result = clock.sync_once(HOST, now=lambda: GOOD_TIME,
                           run=fake_run_for([OLD_TIME, OLD_TIME], commands),
                           ready=lambda: (True, 'P'))
  assert result['status'] == 'verify_failed'
  assert len(commands) == 2
