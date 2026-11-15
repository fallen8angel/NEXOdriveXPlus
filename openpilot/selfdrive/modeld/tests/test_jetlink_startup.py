import ast
from functools import partial
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.modeld.jetlink.startup import initial_connection_wait, prepare_resident_engine, RESIDENT_CAPABILITY
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout, Message


@pytest.mark.parametrize('error', [TimeoutError('HELLO'), LinkTimeout('HELLO'), ConnectionRefusedError('not listening'),
                                 LinkError('gadget write had no reader for 3.000s')])
@pytest.mark.parametrize('elapsed,ready,stage,expected', [
  (0, False, 'hello', True), (119.9, False, 'transport', True), (120, False, 'hello', False),
  (-1, False, 'hello', False), (1, True, 'hello', False), (1, False, 'model', False), (1, False, 'session', False),
])
def test_bounded_startup_only(error, elapsed, ready, stage, expected):
  assert initial_connection_wait(error, elapsed=elapsed, ready_once=ready, stage=stage) is expected


@pytest.mark.parametrize('error', [RuntimeError("USB gadget 'g1' already owns a600000.dwc3"),
                                 LinkError('NCM route via wlan0'), ValueError('model mismatch'),
                                 PermissionError('bind'), LinkError('partial packet'), OSError('USB unplugged')])
def test_real_faults_never_become_startup_wait(error):
  assert not initial_connection_wait(error, elapsed=1, ready_once=False, stage='transport')


def test_resident_wait_survives_slow_state_and_forty_second_prepare_without_reconnect():
  clock, reports, calls = [0.], [], []
  wanted = NS(sha256='a' * 64)
  transport = object()
  def progress(**values):
    if not reports or clock[0] - reports[-1] >= 1.:
      reports.append(clock[0])
    assert values['preparation']['stage'] == 'resident-wait'
  def state(**kwargs):
    assert kwargs['timeout'] <= 2.
    calls.append('STATE')
    clock[0] += 2.
    return {'loaded': wanted.sha256 if clock[0] >= 40. else None, 'engine_state': 'loading'}
  def ensure(model, **kwargs):
    assert model is wanted and kwargs['resident_only'] is True
    calls.append('RESIDENT_BIND')
    return wanted
  client = NS(t=transport, state=state, ensure_engine=ensure)
  result = prepare_resident_engine(client, wanted, {RESIDENT_CAPABILITY: True, 'loaded': None},
    connected=lambda: True, progress=progress, clock=lambda: clock[0], sleep=lambda value: clock.__setitem__(0, clock[0] + value))
  assert result is wanted and client.t is transport and calls[-1] == 'RESIDENT_BIND'
  assert clock[0] >= 40. and max(b - a for a, b in zip(reports, reports[1:], strict=False)) < 3.


def test_unload_between_state_and_bind_returns_to_poll_on_same_link():
  from openpilot.selfdrive.modeld.jetlink.client import EngineMissing
  wanted = NS(sha256='a' * 64)
  calls = []
  def ensure(*args, **kwargs):
    calls.append('bind')
    if len(calls) == 1:
      raise EngineMissing('unloaded after STATE')
    return wanted
  client = NS(state=lambda **kw: {'loaded': wanted.sha256}, ensure_engine=ensure)
  assert prepare_resident_engine(client, wanted, {RESIDENT_CAPABILITY: True, 'loaded': wanted.sha256},
    connected=lambda: True, progress=lambda **kw: None, sleep=lambda _: None) is wanted
  assert calls == ['bind', 'bind']


@pytest.mark.parametrize('response', [{}, {'loaded': None, 'engine_state': 'failed'},
  {'loaded': None, 'nexo_engine_preparation': {'sha256': 'a' * 64, 'state': 'failed', 'detail': 'tactic unavailable'}}])
def test_real_preparation_failures_are_not_hidden_as_wait(response):
  client = NS(state=lambda **kw: response, ensure_engine=lambda *a, **kw: pytest.fail('not resident'))
  with pytest.raises(LinkError):
    prepare_resident_engine(client, NS(sha256='a' * 64), {RESIDENT_CAPABILITY: True},
      connected=lambda: True, progress=lambda **kw: None, sleep=lambda _: None)


def test_legacy_host_is_refused_before_any_setup_request():
  client = NS(ensure_engine=lambda *a, **kw: pytest.fail('legacy host could build onroad'))
  with pytest.raises(LinkError, match='resident-only'):
    prepare_resident_engine(client, NS(sha256='a' * 64), {}, connected=lambda: True, progress=lambda **kw: None)


def test_two_second_state_wait_keeps_hud_pumping_without_changing_deadline(monkeypatch):
  now, hud = [0.], []
  monkeypatch.setattr('openpilot.selfdrive.modeld.jetlink.client.time.monotonic', lambda: now[0])
  client = None
  def receive(timeout):
    now[0] += timeout
    if now[0] < 1.9:
      raise LinkTimeout('slow valid STATE')
    return Message(P.Msg.STATE_RESP, client.seq, 0, memoryview(b'{"loaded":null}'))
  client = JetlinkClient(NS(send_json=lambda *a: None, recv=receive))
  value = client.state(timeout=2., progress=lambda: hud.append(now[0]))
  assert value == {'loaded': None} and now[0] < 2.
  assert len(hud) > 30 and max(b - a for a, b in zip(hud, hud[1:], strict=False)) <= .051
  assert client.deadline == .15


def test_hud_runs_during_resident_wait_and_sleep_without_engine_requests():
  now, hud = [0.], []
  def state(**kwargs):
    assert 'progress' in kwargs
    kwargs['progress']()
    return {'loaded': None}
  client = NS(state=state, ensure_engine=lambda *a, **kw: pytest.fail('unprepared engine request'))
  with pytest.raises(LinkTimeout):
    prepare_resident_engine(client, NS(sha256='a' * 64), {RESIDENT_CAPABILITY: True},
      connected=lambda: True, progress=lambda **kw: None, timeout=1., clock=lambda: now[0],
      sleep=lambda dt: now.__setitem__(0, now[0] + dt), idle=lambda: hud.append(now[0]))
  assert len(hud) >= 20 and max(b - a for a, b in zip(hud, hud[1:], strict=False)) < .051


def test_progress_polling_keeps_original_hello_deadline(monkeypatch):
  now, calls = [0.], []
  monkeypatch.setattr('openpilot.selfdrive.modeld.jetlink.client.time.monotonic', lambda: now[0])
  def recv(timeout):
    now[0] += timeout
    raise LinkTimeout('waiting')
  client = JetlinkClient(NS(send_json=lambda *a, **kw: None, recv=recv))
  with pytest.raises(LinkTimeout):
    client.hello(timeout=5, progress=lambda: calls.append(now[0]))
  assert now[0] == 5 and calls == [0, 1, 2, 3, 4]
  assert client.deadline == .15


def test_progress_does_not_swallow_protocol_error():
  transport = NS(send_json=lambda *a, **kw: None,
                 recv=lambda timeout: Message(P.Msg.HELLO_RESP, 1, 0, memoryview(b'{"protocol":3}')))
  with pytest.raises(LinkError, match='protocol mismatch'):
    JetlinkClient(transport).hello(progress=lambda: None)


def test_daemon_startup_model_phase_and_post_ready_failure(tmp_path):
  """Run the real owner main with fake hardware; observe retry classification."""
  import numpy as np
  source = Path(__file__).resolve().parents[1] / 'jetlink/daemon.py'
  node = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  attempt, records = [0], []
  spec = NS(sha256='native', warped_shape=(1,), packed_nelem=1)
  def mode():
    attempt[0] += 1
    return 'ncm-tcp'
  def connect(mode):
    if attempt[0] == 1:
      raise ConnectionRefusedError('late server')
    if attempt[0] == 4:
      raise TimeoutError('server restart')
    return NS(close=lambda: None, interface='usb1')
  class Client:
    def __init__(self, transport):
      self.t = transport
    def hello(self, progress):
      progress()
      return {'protocol': 2, RESIDENT_CAPABILITY: True, 'loaded': spec.sha256}
    def ensure_engine(self, model, progress, **kwargs):
      assert kwargs.get('resident_only') is True
      progress()
      if attempt[0] == 2:
        raise LinkError('model contract mismatch')
    def infer(self, *a, **kw):
      pass
    def state(self, **kw):
      return {}
    def close(self):
      pass
  def serve(*args, prepare=None):
    prepare(None, {})
    raise LinkError('peer disconnected after ready')
  listener = NS(bind=lambda *a: None, listen=lambda *a: None, settimeout=lambda *a: None, close=lambda: None)
  env = {'Path': Path, 'SOCKET': str(tmp_path / 'ipc'), 'socket': NS(AF_UNIX=1, SOCK_STREAM=1, socket=lambda *a: listener),
         'os': NS(chmod=lambda *a: None), 'time': NS(monotonic=lambda: 10., sleep=lambda *a: None),
         'enabled': lambda: attempt[0] < 4, 'load_spec': lambda: spec,
         'Params': lambda: NS(get_bool=lambda key: False), 'transport_mode': mode, 'connect_transport': connect,
         'JetlinkClient': Client, 'np': np, 'partial': partial, 'serve_modeld': serve, 'initial_connection_wait': initial_connection_wait,
         'prepare_resident_engine': prepare_resident_engine,
         'publish': lambda state, *a, **kw: records.append({'state': state, **kw}),
         'cloudlog': NS(warning=lambda *a: None, exception=lambda *a: None)}
  exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), env)
  env['main']()
  assert any(r['state'] == 'connecting' and r.get('wait_reason') == 'late server' for r in records)
  assert any(r['state'] == 'loading' and r.get('interface') == 'usb1' for r in records)
  assert any(r['state'] == 'retrying' and r.get('stage') == 'model' for r in records)
  assert any(r['state'] == 'retrying' and r.get('stage') == 'session' for r in records)
  assert any(r['state'] == 'retrying' and r.get('error') == 'server restart' for r in records)
