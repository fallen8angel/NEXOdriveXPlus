"""Real stream/session tests; GPU, USB topology and renderer are not hardware tested."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet
from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport
from openpilot.selfdrive.modeld.jetlink.protocol import Msg
from openpilot.tools.jetson import ncm_display as display
from openpilot.tools.jetson.hud import run_owned_session
from openpilot.tools.jetson.protected_probe import ProbeReport


def snapshot():
  return {'version': 1, 'sent': 1., 'events': {}, 'params': {}, 'mono': {}, 'received': {},
          'valid': {}, 'alive': {}, 'cameras': {}}


def message(kind, seq=1, value=None):
  return SimpleNamespace(msg_type=kind, seq=seq, payload=json.dumps(value or {}).encode())


@pytest.fixture
def session(tmp_path, monkeypatch):
  monkeypatch.setattr(display, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(display, 'RUNTIME', tmp_path)
  sent = []
  transport = SimpleNamespace(send_json=lambda *a: sent.append(a), close=lambda: None)
  navi = SimpleNamespace(send=lambda raw: sent.append(raw), close=lambda: None)
  return display.DisplaySession(transport, navi), sent


@pytest.mark.parametrize('kind', [Msg.ENGINE_REQ, Msg.INFER_REQ, Msg.UPLOAD_CHUNK, Msg.SHUTDOWN_REQ, 0x4002])
def test_display_host_rejects_every_model_and_management_operation(session, kind):
  host, sent = session
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  with pytest.raises(ValueError, match='forbidden'):
    host.handle(message(kind, 2))
  assert len(sent) == 1 and sent[0][2][display.DISPLAY_CAPABILITY]


def test_inference_client_and_no_handshake_are_refused(session):
  host, _ = session
  with pytest.raises(ValueError, match='inference clients'):
    host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-jetlink'}}))
  with pytest.raises(ValueError, match='HELLO required'):
    host.handle(message(Msg.HUD, 2))


def test_fresh_hud_and_panel_transmission_are_distinct(session):
  host, sent = session
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  value = {**snapshot(), 'session': 'a' * 32}
  host.handle(message(Msg.HUD, 2, value))
  host.handle(message(Msg.STATE_REQ, 3))
  assert sent[-1][2]['telemetry'] == {'carrot_hud_connected': False, 'hud_data_received': True}
  assert json.loads(display.SNAPSHOT.read_text())['session'] == 'a' * 32
  with pytest.raises(ValueError, match='sequence'):
    host.handle(message(Msg.HUD, 2, value))
  host.close()
  assert not display.SNAPSHOT.exists()


def test_display_opt_in_never_enables_inference(tmp_path, monkeypatch):
  marker = tmp_path / 'opt-in'
  monkeypatch.setattr(display, 'OPT_IN', marker)
  monkeypatch.setattr(display, 'enabled', lambda: False)
  params = SimpleNamespace(get_bool=lambda name: False)
  assert not display.requested(params)
  marker.touch()
  assert display.requested(params)
  assert not display.requested(SimpleNamespace(get_bool=lambda name: True))
  monkeypatch.setattr(display, 'enabled', lambda: True)
  assert not display.requested(params)


@pytest.mark.parametrize('fails', [False, True])
def test_boot_display_lock_is_released_on_data_loss(fails):
  calls = []
  @contextmanager
  def owner(hud):
    assert hud is True
    calls.append('acquire')
    try:
      yield True
    finally:
      calls.append('release')
  def run():
    calls.append('render')
    if fails:
      raise ConnectionError('snapshot expired')
  if fails:
    with pytest.raises(ConnectionError):
      run_owned_session(run, owner)
  else:
    run_owned_session(run, owner)
  assert calls == ['acquire', 'render', 'release']


def test_probe_failure_preserves_specific_phase(tmp_path):
  report = ProbeReport(tmp_path / 'probe.json')
  report.record('engine_build_or_load')
  report.record(state='failed', error='No implementation for Conv + GELU')
  result = json.loads(report.path.read_text())
  assert result['phase'] == 'engine_build_or_load' and not result['physical_hud_verified']
  assert result['state'] == 'failed' and not result['synthetic_inference']


@pytest.mark.parametrize('cycles', [2, 500])
def test_real_tcp_display_reconnect_without_engine(tmp_path, monkeypatch, cycles):
  vendor = Path(os.environ['CARROT_JETSON'])
  monkeypatch.syspath_prepend(str(vendor / 'third_party/jetlink'))
  from openpilot.tools.jetson.ncm_server import transport_class
  monkeypatch.setattr(display, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(display, 'STATUS', tmp_path / 'status.json')
  for _ in range(cycles):  # New stream after comma or Jetson restart.
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    sock = socket.create_connection(listener.getsockname())
    peer, _ = listener.accept()
    listener.close()
    client = JetlinkClient(JetlinkNcmTcpTransport(sock), name='nexo-ncm-display')
    server = display.DisplaySession(transport_class()(peer), SimpleNamespace(send=lambda raw: None, close=lambda: None))
    errors = []
    def serve(server=server, errors=errors):
      try:
        for _ in range(4):  # HELLO, HUD, STATE, STATE.
          server.handle(server.t.recv(1))
      except BaseException as error:
        errors.append(error)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
      hello = client.hello()
      assert hello[display.DISPLAY_CAPABILITY] and hello[display.ACK_CAPABILITY]
      receipt = display.HudReceipt(client.t, supported=True)
      receipt.send(Msg.HUD, client._next_seq(), [snapshot_packet(json.dumps(snapshot()).encode(), 'a' * 32)])
      state = client.state()
      assert state['telemetry']['hud_data_received'] and receipt.check(state)
      assert client.state()['engine'] is None
      thread.join(2)
      assert not thread.is_alive() and not errors
    finally:
      client.close()
      server.close()
      thread.join(2)
    assert not display.SNAPSHOT.exists()


def test_client_refuses_normal_inference_host_before_sending_hud(monkeypatch):
  client = SimpleNamespace(hello=lambda **kw: {display.CAPABILITY: True})
  with pytest.raises(ValueError, match='display-only host'):
    display.send_session(client, None, lambda: True)


def test_real_tcp_signal_faults_keep_base_hud_and_ack(tmp_path, monkeypatch):
  from openpilot.tools.jetson.hud_signals import RemoteSignals
  from openpilot.tools.jetson.tests.test_hud_signals import packet
  monkeypatch.syspath_prepend(str(Path(os.environ['CARROT_JETSON']) / 'third_party/jetlink'))
  from openpilot.tools.jetson.ncm_server import transport_class
  monkeypatch.setattr(display, 'SNAPSHOT', tmp_path / 'hud.json')
  listener = socket.socket()
  listener.bind(('127.0.0.1', 0))
  listener.listen(1)
  sock = socket.create_connection(listener.getsockname())
  peer, _ = listener.accept()
  listener.close()
  client = JetlinkClient(JetlinkNcmTcpTransport(sock), name='nexo-ncm-display')
  server = display.DisplaySession(transport_class()(peer), SimpleNamespace(send=lambda raw: None, close=lambda: None))
  cases = ('red', 'green', 'invalid', 'mismatch', 'no-camera', 'expired', 'restart', 'unknown')
  errors = []
  def serve():
    try:
      for _ in range(1 + len(cases) * 2):
        server.handle(server.t.recv(2))
    except Exception as error:
      errors.append(error)
  worker = threading.Thread(target=serve)
  worker.start()
  try:
    client.hello()
    receipt, reader = display.HudReceipt(client.t, True), RemoteSignals()
    for index, case in enumerate(cases):
      value = packet(stamp=100 + index, sent=100.1 + index, fid=index,
                     raw=case if case in ('red', 'green') else 'unknown')
      if case == 'invalid':
        value['signal_display'] = {'version': 2, 'record': []}
      elif case == 'mismatch':
        value['cameras']['road']['frame'] += 1
      elif case == 'no-camera':
        value['cameras'] = {}
      elif case == 'expired':
        value['sent'] += 1
      elif case == 'restart':
        value['signal_display']['record'].update(session='restarted-observer', frame_id=0)
        value['cameras']['road']['frame'] = 0
      receipt.send(Msg.HUD, client._next_seq(), [snapshot_packet(json.dumps(value).encode(), 'a' * 32)])
      state = client.state()
      assert receipt.check(state) and state['telemetry']['hud_data_received'] and state['engine'] is None
      received = json.loads(display.SNAPSHOT.read_text())
      assert received['params']['IsOnroad'] == 'MQ=='
      lights = reader.read(received, time.monotonic())
      assert bool(lights) == (case in ('red', 'green', 'restart', 'unknown'))
      if lights:
        assert lights[0]['kind'] == (case if case in ('red', 'green') else 'unknown')
    worker.join(3)
    assert not worker.is_alive() and not errors
  finally:
    client.close()
    server.close()
    worker.join(3)
  assert not display.SNAPSHOT.exists()


def test_host_dispatch_does_not_import_gpu(monkeypatch):
  from openpilot.tools.jetson import native_host
  monkeypatch.setenv('NEXO_JETLINK_DISPLAY_ONLY', '1')
  monkeypatch.setattr(native_host.Path, 'is_file', lambda self: True)
  calls = []
  monkeypatch.setattr(display, 'host_main', lambda: calls.append('display'))
  monkeypatch.setitem(sys.modules, 'tensorrt', None)
  native_host.main()
  assert calls == ['display']


def test_status_reports_display_connection_without_inference(tmp_path, monkeypatch):
  from openpilot.selfdrive.carrot.server.services import jetson
  monkeypatch.setattr(jetson, 'NCM_DISPLAY_STATUS', tmp_path / 'ncm.json')
  monkeypatch.setattr(jetson, 'DISPLAY_STATUS', tmp_path / 'old.json')
  monkeypatch.setattr(jetson, 'LINK_STATUS', ())
  monkeypatch.setattr(jetson, 'MODEL_STATUS', ())
  monkeypatch.setattr(jetson, 'UDC_ROOT', tmp_path / 'udc')
  jetson.NCM_DISPLAY_STATUS.write_text(json.dumps({'updated': 100., 'state': 'connected',
    'peer': {'telemetry': {'carrot_hud_connected': True}}, 'last_hud_tx_mono': 100.,
    'hud_enabled': True, 'capability_negotiated': True}))
  result = jetson.snapshot({}, now=100.1)
  assert result['connected'] and result['status']['usb_connected'] and result['hud_tx_recent']
  assert result['status']['model_active'] is False and result['status']['model_ready'] is False
  assert not jetson.snapshot({}, now=102.)['connected']


def test_display_candidate_probe_never_imports_tensorrt(tmp_path, monkeypatch):
  from openpilot.tools.jetson import protected_probe, hud
  vendor = Path(os.environ['CARROT_JETSON'])
  monkeypatch.setenv('CARROT_JETSON', str(vendor))  # Probe changes its own child environment.
  monkeypatch.setattr(sys, 'path', list(sys.path))
  monkeypatch.syspath_prepend(str(vendor / 'tools/jetlink'))
  monkeypatch.syspath_prepend(str(vendor / 'third_party/jetlink'))
  monkeypatch.setitem(sys.modules, 'tensorrt', None)
  monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(main=lambda: None))
  monkeypatch.setattr(hud, 'install_adapters', lambda **kw: None)  # Linux renderer imports separately required on Jetson.
  report = ProbeReport(tmp_path / 'probe.json', display_only=True)
  protected_probe.run_probe(report)
  assert report.value['state'] == 'passed' and not report.value['synthetic_inference']
  assert report.value['phase'] == 'display_software_checked' and not report.value['physical_hud_verified']


@pytest.mark.parametrize('phase', ['hud_imports', 'engine_build_or_load', 'engine_output_contract'])
def test_probe_main_keeps_failure_stage_for_installer(tmp_path, monkeypatch, phase):
  from openpilot.tools.jetson import protected_probe
  monkeypatch.setattr(sys, 'argv', ['probe', str(tmp_path)])
  def fail(report):
    report.record(phase)
    raise RuntimeError('candidate failed')
  monkeypatch.setattr(protected_probe, 'run_probe', fail)
  with pytest.raises(RuntimeError, match='candidate failed'):
    protected_probe.main()
  record = json.loads((tmp_path / 'nexo-probe.json').read_text())
  assert record['state'] == 'failed' and record['phase'] == phase


@pytest.mark.parametrize('handshake', [False, True])
def test_session_deadline_rejects_late_or_partial_peer(session, handshake):
  host, _ = session
  now = [100.]
  host.clock, host.last_activity = lambda: now[0], now[0]
  if handshake:
    host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  limit = display.IDLE_TIMEOUT if handshake else display.HELLO_TIMEOUT
  now[0] += limit - .01
  host.check_alive()
  # Partial bytes must not extend the complete-message activity deadline.
  now[0] += .02
  with pytest.raises(TimeoutError, match='idle' if handshake else 'HELLO'):
    host.check_alive()
  with pytest.raises(TimeoutError):
    host.handle(message(Msg.STATE_REQ, 2))


def test_idle_deadline_moves_on_valid_state_and_repeated_hello_is_refused(session):
  host, _ = session
  now = [100.]
  host.clock, host.last_activity = lambda: now[0], now[0]
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  for seq in range(2, 10):
    now[0] += display.IDLE_TIMEOUT - .1
    host.handle(message(Msg.STATE_REQ, seq))
  with pytest.raises(ValueError, match='already completed'):
    host.handle(message(Msg.HELLO_REQ, 10, {'client': {'name': 'nexo-ncm-display'}}))


def test_host_receipt_only_advances_after_successful_snapshot_write(session, monkeypatch):
  host, sent = session
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  host.handle(message(Msg.STATE_REQ, 2))
  assert sent[-1][2]['hud_rx_seq'] is None and sent[-1][2]['hud_rx_age'] is None
  def fail(*args):
    raise OSError('snapshot storage failed')
  monkeypatch.setattr(display, 'atomic_json', fail)
  with pytest.raises(OSError):
    host.handle(message(Msg.HUD, 3, {**snapshot(), 'session': 'a' * 32}))
  assert host.last_hud is None and host.last_hud_seq is None


def test_receipt_age_uses_host_clock_without_comparing_comma_clock(session):
  host, sent = session
  now = [9000.]
  host.clock, host.last_activity = lambda: now[0], now[0]
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  host.handle(message(Msg.HUD, 2, {**snapshot(), 'session': 'a' * 32}))
  now[0] += .75
  host.handle(message(Msg.STATE_REQ, 3))
  state = sent[-1][2]
  assert state['hud_rx_seq'] == 2 and state['hud_rx_age'] == .75
  assert not state['telemetry']['hud_data_received']


@pytest.mark.parametrize('seq,age', [(None, None), (1, .1), (3, .1), (True, .1), (2., .1),
                                   (2, -1), (2, float('nan')), (2, float('inf')), (2, True), (2, None)])
def test_missing_old_future_or_invalid_acknowledgement_invalidates_session(seq, age):
  receipt = display.HudReceipt(SimpleNamespace(send=lambda *a, **kw: None), True)
  receipt.send(Msg.HUD, 2, [b'snapshot'])
  with pytest.raises(ValueError, match='acknowledgement|receipt age'):
    receipt.check({'hud_rx_seq': seq, 'hud_rx_age': age})


def test_receipt_does_not_confuse_media_or_stale_receipt_with_fresh_hud():
  receipt = display.HudReceipt(SimpleNamespace(send=lambda *a, **kw: None), True)
  assert not receipt.check({'hud_rx_seq': None, 'hud_rx_age': None})
  receipt.send(Msg.HUD, 2, [b'snapshot'])
  receipt.send(Msg.NAVI_MEDIA, 3, [b'media'])
  assert receipt.check({'hud_rx_seq': 2, 'hud_rx_age': .4})
  assert receipt.fresh_for == pytest.approx(.1)
  assert not receipt.check({'hud_rx_seq': 2, 'hud_rx_age': .5})
  with pytest.raises(ValueError, match='different session'):
    display.HudReceipt(receipt.transport, True).check({'hud_rx_seq': 2, 'hud_rx_age': .1})


def test_failed_write_is_not_marked_as_sent():
  def fail(*a, **kw):
    raise ConnectionError('USB disconnected')
  receipt = display.HudReceipt(SimpleNamespace(send=fail), True)
  with pytest.raises(ConnectionError):
    receipt.send(Msg.HUD, 2, [b'snapshot'])
  assert receipt.last_seq is None


@pytest.mark.parametrize('acknowledgement', [True, False])
@pytest.mark.parametrize('discard_hud', [True, False])
def test_client_ack_negotiation_legacy_compatibility_and_stalled_receiver(monkeypatch, acknowledgement, discard_hud):
  now, seq, received_seq, records = [100.], [0], [None], []
  def next_seq():
    seq[0] += 1
    return seq[0]
  def send(kind, number, payload, **kw):
    assert kind == Msg.HUD  # This session must never prepare or run a model.
    if not discard_hud:
      received_seq[0] = number
  def state(**kw):
    return {'hud_rx_seq': received_seq[0], 'hud_rx_age': None if received_seq[0] is None else .01}
  client = SimpleNamespace(t=SimpleNamespace(send=send), _next_seq=next_seq, state=state,
    hello=lambda **kw: {display.DISPLAY_CAPABILITY: True, display.CAPABILITY: True, display.ACK_CAPABILITY: acknowledgement})
  publisher = SimpleNamespace(process=SimpleNamespace(poll=lambda: None), status={},
    send=lambda sender: sender.t.send(Msg.HUD, sender._next_seq(), [b'snapshot']))
  monkeypatch.setattr(display, 'publish', lambda status, **values: records.append(values))
  def sleep(seconds):
    now[0] += .1
  def run():
    display.send_session(client, publisher, lambda: now[0] < 101., clock=lambda: now[0], sleep=sleep)
  if acknowledgement and discard_hud:
    with pytest.raises(ValueError, match='acknowledgement mismatch'):
      run()  # main() closes the stream/producer and retries after this error.
  else:
    run()
  assert records and records[-1]['hud_ack_supported'] is acknowledgement
  assert any(r['hud_rx_confirmed'] for r in records) is (acknowledgement and not discard_hud)


def test_cleanup_releases_navi_and_snapshot_even_if_transport_close_fails(session):
  host, _ = session
  calls = []
  display.SNAPSHOT.write_text('stale')
  def fail():
    raise OSError('transport close failed')
  host.t.close, host.navi.close = fail, lambda: calls.append('closed')
  with pytest.raises(OSError):
    host.close()
  assert calls == ['closed'] and not display.SNAPSHOT.exists()


@pytest.mark.parametrize('handshake', [False, True])
def test_receive_loop_releases_silent_or_partial_stream(session, handshake):
  host, _ = session
  now = [0.]
  host.clock, host.last_activity = lambda: now[0], now[0]
  if handshake:
    host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  class IncompleteMessage(Exception):
    pass
  def recv(timeout):
    now[0] += timeout
    raise IncompleteMessage()
  host.t.recv = recv
  with pytest.raises(TimeoutError, match='idle' if handshake else 'HELLO'):
    display.receive_session(host, IncompleteMessage)
  assert now[0] <= (display.IDLE_TIMEOUT if handshake else display.HELLO_TIMEOUT) + .1


@pytest.mark.parametrize('failures,seconds', [(0, 1), (1, 2), (2, 4), (3, 8), (5, 8), (50, 8)])
def test_hud_only_ncm_reconnect_remains_bounded(failures, seconds):
  assert display.hud_reconnect_delay(failures) == seconds
  assert display.MAX_HUD_RECONNECT_DELAY == 8


def test_display_retry_closes_previous_session_and_preserves_disabled_inference(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import client as client_module
  calls, attempts = [], []
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', SimpleNamespace(Params=lambda: None))
  monkeypatch.setattr(display, 'requested', lambda params: len(attempts) < 2)
  monkeypatch.setattr(display, 'connect_display_ncm', lambda: object())
  def client(transport, name):
    assert name == 'nexo-ncm-display'
    return SimpleNamespace(close=lambda: calls.append('client closed'))
  monkeypatch.setattr(client_module, 'JetlinkClient', client)
  monkeypatch.setattr(display, 'DisplayPublisher', lambda: SimpleNamespace(close=lambda: calls.append('publisher closed')))
  def send(*args):
    attempts.append(True)
    if len(attempts) == 1:
      raise ValueError('NCM display acknowledgement mismatch')
  monkeypatch.setattr(display, 'send_session', send)
  monkeypatch.setattr(display.time, 'sleep', lambda delay: calls.append(('retry delay', delay)))
  monkeypatch.setattr(display, 'publish', lambda state, **values: calls.append(state))
  display.main()
  assert len(attempts) == 2 and calls.count('client closed') == calls.count('publisher closed') == 2
  assert 'retrying' in calls and calls[-1] == 'disabled'
  assert ('retry delay', 2) in calls


def test_slow_state_roundtrip_is_included_in_receipt_freshness(monkeypatch):
  now, seq, received, records = [100.], [0], [None], []
  def next_seq():
    seq[0] += 1
    return seq[0]
  def send(kind, number, payload, **kw):
    received[0] = number
  def state(**kw):
    now[0] += .4
    return {'hud_rx_seq': received[0], 'hud_rx_age': .2 if received[0] is not None else None}
  client = SimpleNamespace(t=SimpleNamespace(send=send), _next_seq=next_seq, state=state,
    hello=lambda **kw: {display.DISPLAY_CAPABILITY: True, display.CAPABILITY: True, display.ACK_CAPABILITY: True})
  publisher = SimpleNamespace(process=SimpleNamespace(poll=lambda: None), status={},
    send=lambda sender: sender.t.send(Msg.HUD, sender._next_seq(), [b'snapshot']))
  monkeypatch.setattr(display, 'publish', lambda status, **values: records.append(values))
  display.send_session(client, publisher, lambda: now[0] < 101., clock=lambda: now[0], sleep=lambda _: None)
  assert len(records) == 3 and not any(r['hud_rx_confirmed'] for r in records)


def test_hud_state_reports_fresh_panel_and_bounded_recovery(session):
  from openpilot.tools.jetson.state import atomic_json
  host, sent = session
  now = time.monotonic()
  atomic_json(display.RUNTIME / 'hud-status.json', {'updated': now, 'pid': 321,
                                                     'last_panel_send_mono': now})
  atomic_json(display.RUNTIME / 'hud-recovery.json',
              {'updated': now, 'attempts': 4, 'failures': 2, 'cooldown_s': 8,
               'last_error': 'panel reset failed', 'password': 'never-include'})
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  host.handle(message(Msg.HUD, 2, {**snapshot(), 'session': 'a' * 32}))
  host.handle(message(Msg.STATE_REQ, 3))
  state = sent[-1][2]
  assert state['telemetry']['carrot_hud_connected'] is True
  assert state['hud_pipeline']['panel_write_recent'] is True
  assert state['hud_pipeline']['renderer_pid'] == 321
  assert 0 <= state['hud_pipeline']['last_panel_send_age_s'] < 2
  assert state['hud_pipeline']['physical_hud_verified'] is False
  recovery = state['telemetry']['nexo_hud_recovery']
  assert recovery['attempts'] == 4 and recovery['failures'] == 2
  assert recovery['cooldown_s'] == 8 and recovery['last_error'] == 'panel reset failed'
  assert 'never-include' not in json.dumps(state)
  host.close()


def test_hud_state_expires_stale_renderer_and_recovery(session):
  from openpilot.tools.jetson.state import atomic_json
  host, sent = session
  now = time.monotonic()
  atomic_json(display.RUNTIME / 'hud-status.json',
              {'updated': now - 5, 'pid': 200, 'last_panel_send_mono': now - 5})
  atomic_json(display.RUNTIME / 'hud-recovery.json',
              {'updated': now - 3601, 'last_error': 'outdated'})
  host.handle(message(Msg.HELLO_REQ, value={'client': {'name': 'nexo-ncm-display'}}))
  host.handle(message(Msg.STATE_REQ, 2))
  state = sent[-1][2]
  assert state['telemetry']['carrot_hud_connected'] is False
  assert 'nexo_hud_recovery' not in state['telemetry']
  assert state['hud_pipeline']['renderer_pid'] is None
  assert state['hud_pipeline']['last_panel_send_age_s'] is None
  assert not state['hud_pipeline']['panel_write_recent']
  host.close()


@pytest.mark.parametrize('panel,state', [(False, 'ERROR'), (True, 'OK'), (None, 'WAITING')])
def test_7000_hud_stage_separates_tx_from_jetson_panel_report(tmp_path, monkeypatch, panel, state):
  from openpilot.selfdrive.carrot.server.services import jetson
  monkeypatch.setattr(jetson, 'NCM_DISPLAY_STATUS', tmp_path / 'ncm.json')
  monkeypatch.setattr(jetson, 'DISPLAY_STATUS', tmp_path / 'legacy.json')
  monkeypatch.setattr(jetson, 'LINK_STATUS', ())
  monkeypatch.setattr(jetson, 'MODEL_STATUS', ())
  monkeypatch.setattr(jetson, 'UDC_ROOT', tmp_path / 'udc')
  peer = {'hud_pipeline': {'renderer_pid': 88, 'panel_write_recent': panel is True,
                           'last_panel_send_age_s': .1, 'physical_hud_verified': False},
          'telemetry': {} if panel is None else {'carrot_hud_connected': panel}}
  jetson.NCM_DISPLAY_STATUS.write_text(json.dumps({
    'updated': 100., 'state': 'connected', 'peer': peer,
    'last_hud_tx_mono': 100., 'hud_enabled': True, 'capability_negotiated': True}))
  result = jetson.snapshot({}, now=100.1)
  hud = next(item for item in result['stages'] if item['id'] == 'hud')
  assert result['hud_tx_recent'] is True and hud['state'] == state
  assert result['hud_output_reported'] is panel
  assert result['hud_pipeline']['physical_hud_verified'] is False if panel is not None else not result['hud_pipeline']
