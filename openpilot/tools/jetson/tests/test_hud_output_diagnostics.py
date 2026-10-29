"""Reproduce missing recovery reports, startup stalls and delayed STATE replies."""
import json
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import health, hud, hud_diagnostics, hud_events, ncm_display, protected_install, supervisor
from openpilot.tools.jetson.state import atomic_json


@pytest.mark.parametrize('error,reason', [(PermissionError(13, 'denied'), 'usb_permission'),
  (OSError(16, 'busy'), 'panel_busy'), (OSError(19, 'gone'), 'usb_disconnected'),
  (RuntimeError('sync command timed out'), 'usb_open_failed')])
def test_usb_open_errors_retain_stage_and_category(error, reason, tmp_path, monkeypatch):
  monkeypatch.setattr(hud_events, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud_events, '_stage', '')
  class Panel:
    def open(self):
      raise error
  with pytest.raises(hud_events.PanelFailure) as caught:
    hud.display_class(Panel)().open()
  assert hud.failure_reason(caught.value) == reason
  assert json.loads((tmp_path / 'hud-stage.json').read_text())['stage'] == 'usb_open'


def test_import_failure_before_cluster_main_is_recorded(tmp_path, monkeypatch):
  monkeypatch.setattr(hud, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud_events, 'RUNTIME', tmp_path)
  monkeypatch.setattr(hud.sys, 'argv', ['hud', '--renderer-child'])
  def fail():
    hud_events.record('adapter_import')
    raise ImportError('missing renderer dependency')
  monkeypatch.setattr(hud, '_main', fail)
  with pytest.raises(ImportError):
    hud.main()
  record = json.loads((tmp_path / 'hud-exit.json').read_text())
  assert record['stage'] == 'adapter_import' and record['error_type'] == 'ImportError'
  assert record['error_time_unix'] > 0


def test_missing_and_stale_supervisor_are_not_empty_success(tmp_path):
  result = hud_diagnostics.renderer_evidence(100, tmp_path)
  assert result['recovery']['report_status'] == 'missing'
  assert result['recovery']['blocked'] is None and result['renderer_running'] is None
  atomic_json(tmp_path / 'hud-service.json', {'updated': 100, 'available': True, 'ActiveState': 'failed', 'ExecMainStatus': '1'})
  result = hud_diagnostics.renderer_evidence(100, tmp_path)
  assert result['recovery']['last_error'] == 'hud_service_failed'
  atomic_json(tmp_path / 'hud-recovery.json', {'updated': 90, 'renderer_pid': 10, 'state': 'running'})
  result = hud_diagnostics.renderer_evidence(100, tmp_path)
  assert result['renderer_pid'] is None and result['recovery']['report_status'] == 'stale'


def test_live_renderer_without_first_panel_write_has_pid(tmp_path):
  atomic_json(tmp_path / 'hud-recovery.json', {'updated': 100, 'renderer_pid': 10, 'renderer_stage': 'usb_open', 'state': 'running'})
  result = hud_diagnostics.renderer_evidence(100, tmp_path)
  assert result['renderer_pid'] == 10 and result['renderer_running']
  assert result['renderer_stage'] == 'usb_open' and result['last_successful_panel_age_s'] is None


def test_service_sampler_is_read_only_and_reports_timeout(monkeypatch):
  seen = []
  def run(command, **kwargs):
    seen.append(command)
    return SimpleNamespace(stdout='ActiveState=failed\nExecMainStatus=203\nSecret=private\n')
  monkeypatch.setattr(hud_diagnostics.subprocess, 'run', run)
  assert hud_diagnostics.service_state() == {'available': True, 'ActiveState': 'failed', 'ExecMainStatus': '203'}
  assert seen[0][:3] == ['systemctl', 'show', 'carrot-jetlink-hud.service']
  monkeypatch.setattr(hud_diagnostics.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(OSError('unavailable')))
  assert not hud_diagnostics.service_state()['available']


class Child:
  pid = 123
  returncode = None
  def poll(self):
    return self.returncode


def make_supervisor(tmp_path, monkeypatch):
  now, children = [100.], []
  monkeypatch.setattr(supervisor, 'RUNTIME', tmp_path)
  monkeypatch.setattr(supervisor, 'stop_process', lambda *a: True)
  def spawn(*a, **k):
    children.append(Child())
    return children[-1]
  return supervisor.RendererSupervisor([], lambda: True, lambda: now[0], spawn), now, children


def test_usb_startup_is_bounded_but_not_killed_at_steady_state_deadline(tmp_path, monkeypatch):
  obj, now, children = make_supervisor(tmp_path, monkeypatch)
  obj.tick()
  atomic_json(tmp_path / 'hud-stage.json', {'updated': 108, 'pid': 123, 'stage': 'usb_open'})
  now[0] = 111
  assert obj.tick()['failures'] == 0
  now[0] = 129
  state = obj.tick()
  assert state['failures'] == 1 and state['failure_stage'] == 'usb_open'
  assert state['last_error_time_unix'] > 0


def test_usb_busy_gets_only_two_timed_probes_without_replug(tmp_path, monkeypatch):
  obj, now, children = make_supervisor(tmp_path, monkeypatch)
  for _ in range(3):
    obj.tick()
    children[-1].returncode = 76
    obj.tick()
    now[0] += 30
  assert len(children) == 3
  for expected in (4, 5):
    now[0] = obj.retry_after + 1
    obj.tick()
    assert len(children) == expected
    children[-1].returncode = 76
    obj.tick()
  now[0] += 3600
  assert obj.tick()['blocked'] and len(children) == 5
  assert obj.timed_probes == 2
  restarted = supervisor.RendererSupervisor([], lambda: True, lambda: now[0])
  assert restarted.timed_probes == 2 and restarted.error == 'panel_busy'


def test_healthy_panel_does_not_restart_and_preserves_last_success(tmp_path, monkeypatch):
  obj, now, children = make_supervisor(tmp_path, monkeypatch)
  obj.tick()
  for _ in range(100):
    now[0] += 1
    atomic_json(tmp_path / 'hud-status.json', {'updated': now[0], 'pid': 123, 'last_panel_send_mono': now[0]})
    obj.tick()
  assert len(children) == 1 and obj.failures == 0
  (tmp_path / 'hud-status.json').unlink()
  children[-1].returncode = 75
  obj.tick()
  assert obj.last_successful_panel == 200


def test_7000_reports_failure_stage_and_versions_and_ages_out_remote_pid(tmp_path, monkeypatch):
  monkeypatch.setattr(health, 'RUNTIME', tmp_path)
  monkeypatch.setattr(health, 'source_revision', lambda: 'a' * 40)
  peer = {'source_revision': 'b' * 40, 'hud_rx_age': .1,
          'hud_pipeline': {'renderer_pid': 123, 'renderer_running': True, 'renderer_stage': 'usb_open', 'panel_write_recent': False},
          'hud_diagnostics': {'recovery': {'available': True, 'report_status': 'fresh', 'state': 'running',
             'blocked': False, 'last_error': 'usb_open_failed', 'last_error_time_unix': 1700000000},
             'service': {'available': True, 'ActiveState': 'active'}}}
  atomic_json(tmp_path / 'ncm-display.json', {'updated': 100, 'state': 'connected', 'last_peer_rx_mono': 100,
              'last_hud_tx_mono': 100, 'peer': peer})
  evidence = health.diagnostic_evidence(100)
  assert evidence['stopped_stage'] == 'usb_open' and evidence['renderer_running']
  assert evidence['recovery']['last_error_time_unix'] == 1700000000
  assert evidence['comma_revision'] == 'a' * 40 and evidence['jetson_revision'] == 'b' * 40
  assert not evidence['physical_hud_verified'] and not evidence['panel_write_reported']
  assert health.diagnostic_evidence(103)['renderer_pid'] is None
  peer.pop('hud_diagnostics')
  atomic_json(tmp_path / 'ncm-display.json', {'updated': 100, 'state': 'connected', 'last_peer_rx_mono': 100, 'peer': peer})
  assert health.diagnostic_evidence(100)['recovery']['report_status'] == 'not_reported'


@pytest.mark.parametrize('display_only,server,hud_state,allowed', [
  (True, 'active', 'failed', True), (True, 'active', 'inactive', True),
  (False, 'active', 'failed', False), (True, 'failed', 'failed', False),
  (True, 'active', 'activating', False)])
def test_protected_repair_only_admits_stopped_display_only_hud(monkeypatch, display_only, server, hud_state, allowed):
  monkeypatch.setattr(protected_install, 'active', lambda: False)
  monkeypatch.setattr(protected_install, 'run', lambda *a: server if a[2] == protected_install.UNITS[0] else hud_state)
  if allowed:
    protected_install.check_services_for_update(display_only)
  else:
    with pytest.raises(RuntimeError):
      protected_install.check_services_for_update(display_only)


def test_state_deadline_and_wrong_reply_do_not_reuse_session():
  now = [100.]
  def recv(**kw):
    raise ncm_display.LinkTimeout('pending')
  client = SimpleNamespace(t=SimpleNamespace(send=lambda *a, **kw: None, recv=recv), _next_seq=lambda: 1)
  poll = ncm_display.StatePoll(client, lambda: now[0])
  receipt = ncm_display.HudReceipt(client.t, True)
  assert poll.poll(receipt) is None
  now[0] = 100.5
  with pytest.raises(TimeoutError, match='STATE response'):
    poll.poll(receipt)


def test_offline_install_script_preserves_config_and_checks_hash(tmp_path):
  import hashlib
  from openpilot.tools.jetson.install_hud_update import install_command
  config = tmp_path / 'nexo-ncm.json'
  config.write_text(json.dumps({'display_only': True, 'transport': 'ncm-tcp', 'interface': 'enx123', 'model_sha256': 'f' * 64}))
  for name in ('protected_install.py', 'protected_ncm.py'):
    (tmp_path / name).touch()
  bundle = tmp_path / 'bundle.tar.gz'
  bundle.write_bytes(b'bundle')
  command = install_command(tmp_path, bundle, hashlib.sha256(b'bundle').hexdigest(), config)
  assert command[command.index('--ncm-interface') + 1] == 'enx123'
  assert command[command.index('--model-sha256') + 1] == 'f' * 64
  with pytest.raises(ValueError, match='SHA256'):
    install_command(tmp_path, bundle, '0' * 64, config)


def test_real_tcp_delayed_state_replies_do_not_pause_hud_writes(monkeypatch):
  import socket
  import threading
  import time
  from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
  from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport
  from openpilot.selfdrive.modeld.jetlink.protocol import Msg
  listener = socket.socket()
  listener.bind(('127.0.0.1', 0))
  listener.listen(1)
  sock = socket.create_connection(listener.getsockname())
  accepted, _ = listener.accept()
  listener.close()
  client = JetlinkClient(JetlinkNcmTcpTransport(sock), name='nexo-ncm-display')
  server = JetlinkNcmTcpTransport(accepted)
  stopped, hud_times, errors = threading.Event(), [], []
  def receive():
    pending, last_hud = [], None
    try:
      while not stopped.is_set():
        now = time.monotonic()
        while pending and pending[0][0] <= now:
          _, seq, value = pending.pop(0)
          server.send_json(Msg.STATE_RESP, seq, value)
        try:
          msg = server.recv(.005)
        except ncm_display.LinkTimeout:
          continue
        if msg.msg_type == Msg.HELLO_REQ:
          server.send_json(Msg.HELLO_RESP, msg.seq, {'protocol': 2, ncm_display.DISPLAY_CAPABILITY: True,
                            ncm_display.CAPABILITY: True, ncm_display.ACK_CAPABILITY: True})
        elif msg.msg_type == Msg.HUD:
          last_hud = msg.seq
          hud_times.append(time.monotonic())
        elif msg.msg_type == Msg.STATE_REQ:
          pending.append((now + .35, msg.seq, {'hud_rx_seq': last_hud, 'hud_rx_age': .01 if last_hud is not None else None}))
        else:
          raise AssertionError('Non-display traffic')
    except Exception as error:
      errors.append(error)
  thread = threading.Thread(target=receive)
  thread.start()
  monkeypatch.setattr(ncm_display, 'publish', lambda *a, **kw: None)
  publisher = SimpleNamespace(status={}, send=lambda sender: sender.t.send(Msg.HUD, sender._next_seq(), [b'fresh'], timeout=.012))
  try:
    end = time.monotonic() + .9
    ncm_display.send_session(client, publisher, lambda: time.monotonic() < end)
  finally:
    stopped.set()
    thread.join(timeout=2)
    client.close()
    server.close()
  assert not thread.is_alive() and not errors
  assert len(hud_times) >= 15
  assert max(b - a for a, b in zip(hud_times, hud_times[1:])) < .25
