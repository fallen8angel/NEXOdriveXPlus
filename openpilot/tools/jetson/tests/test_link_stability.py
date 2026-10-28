import json

from openpilot.tools.jetson import process, supervisor, hud


class Child:
  pid = 123
  returncode = None
  def poll(self):
    return self.returncode
  def terminate(self):
    self.returncode = -15
  def kill(self):
    self.returncode = -9
  def wait(self, timeout=None):
    return self.returncode


def make_supervisor(tmp_path, monkeypatch):
  now, wanted, children = [100.], [True], []
  monkeypatch.setattr(supervisor, 'RUNTIME', tmp_path)
  monkeypatch.setattr(process, 'RUNTIME', tmp_path)
  monkeypatch.setattr(supervisor, 'stop_process', lambda child, *args: child.terminate() is None)
  def spawn(*args, **kwargs):
    children.append(Child())
    return children[-1]
  obj = supervisor.RendererSupervisor(['renderer'], lambda: wanted[0], clock=lambda: now[0], spawn=spawn)
  return obj, now, wanted, children


def test_transient_and_missing_input_do_not_kill_output(tmp_path, monkeypatch):
  obj, now, wanted, children = make_supervisor(tmp_path, monkeypatch)
  obj.tick()
  wanted[0] = False
  now[0] += 60
  obj.tick()
  wanted[0] = True
  obj.tick()
  now[0] += 9.9
  obj.tick()
  assert len(children) == 1 and children[0].poll() is None
  now[0] += .2
  assert obj.tick()['failures'] == 1
  assert children[0].poll() is not None


def test_repeated_output_failure_stops_after_three_attempts(tmp_path, monkeypatch):
  obj, now, _, children = make_supervisor(tmp_path, monkeypatch)
  for _ in range(3):
    obj.tick()
    children[-1].returncode = 1
    obj.tick()
    now[0] += 30
  assert obj.tick()['blocked'] is True
  now[0] += 3600
  obj.tick()
  assert len(children) == 3


def test_receipt_is_not_panel_proof_and_pid_must_match(tmp_path, monkeypatch):
  obj, now, _, children = make_supervisor(tmp_path, monkeypatch)
  obj.tick()
  for elapsed in range(12):
    now[0] = 100. + elapsed
    (tmp_path / 'hud-status.json').write_text(json.dumps({
      'updated': now[0], 'last_panel_send_mono': now[0], 'pid': children[0].pid + 1}))
    obj.tick()
  assert obj.successes == 0 and obj.failures == 1


def test_continuous_panel_writes_confirm_recovery(tmp_path, monkeypatch):
  obj, now, _, children = make_supervisor(tmp_path, monkeypatch)
  obj.tick()
  for elapsed in range(40):
    now[0] = 100. + elapsed
    (tmp_path / 'hud-status.json').write_text(json.dumps({
      'updated': now[0], 'last_panel_send_mono': now[0], 'pid': children[0].pid}))
    obj.tick()
  assert len(children) == 1 and obj.successes == 0 and obj.failures == 0


def test_delayed_panel_and_service_restart_preserve_retry_budget(tmp_path, monkeypatch):
  obj, now, _, children = make_supervisor(tmp_path, monkeypatch)
  obj.device_ready = lambda: False
  for elapsed in range(60):
    now[0] = 100. + elapsed
    assert obj.tick()['state'] == 'waiting_panel'
  assert not children and obj.attempts == 0
  obj.device_ready = lambda: True
  now[0] += 1
  obj.tick()
  children[0].returncode = 1
  obj.tick()
  restarted, _, _, _ = make_supervisor(tmp_path, monkeypatch)
  # make_supervisor's clock predates the record, so use the current boot time.
  restarted = supervisor.RendererSupervisor(['renderer'], lambda: True, clock=lambda: now[0])
  assert restarted.consecutive == 1 and restarted.failures == 1


def test_success_count_requires_a_recovery_and_fresh_writes(tmp_path, monkeypatch):
  obj, now, _, children = make_supervisor(tmp_path, monkeypatch)
  obj.tick()
  children[0].returncode = 1
  obj.tick()
  now[0] += 3
  obj.tick()
  for _ in range(4):
    now[0] += 1
    (tmp_path / 'hud-status.json').write_text(json.dumps({
      'updated': now[0], 'last_panel_send_mono': now[0], 'pid': children[-1].pid}))
    obj.tick()
  assert obj.successes == 1 and obj.failures == 1


def test_short_snapshot_gap_waits_without_accepting_stale_data(monkeypatch):
  now = [0.]
  monkeypatch.setattr(hud, 'read_snapshot', lambda: {'fresh': True} if now[0] >= .3 else None)
  assert hud.wait_for_snapshot(clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)) == {'fresh': True}
  assert .3 <= now[0] < .4


