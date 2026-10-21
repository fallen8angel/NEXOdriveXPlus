"""Real stream/session tests; GPU, USB topology and renderer are not hardware tested."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import threading
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


def test_real_tcp_display_reconnect_without_engine(tmp_path, monkeypatch):
  vendor = Path(os.environ['CARROT_JETSON'])
  monkeypatch.syspath_prepend(str(vendor / 'third_party/jetlink'))
  from openpilot.tools.jetson.ncm_server import transport_class
  monkeypatch.setattr(display, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(display, 'STATUS', tmp_path / 'status.json')
  for _ in range(2):  # New stream after comma or Jetson restart.
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
      assert client.hello()[display.DISPLAY_CAPABILITY]
      client.t.send(Msg.HUD, client._next_seq(), [snapshot_packet(json.dumps(snapshot()).encode(), 'a' * 32)])
      assert client.state()['telemetry']['hud_data_received']
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
