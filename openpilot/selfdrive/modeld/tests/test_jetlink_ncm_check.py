from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.jetlink import check_ncm as check


@pytest.mark.parametrize('backend,loaded,display_only,eligible', [
  ('trt', 'a' * 64, False, True), ('trt', 'b' * 64, False, False),
  ('trt', None, False, False), ('ort', 'a' * 64, False, False), ('trt', 'a' * 64, True, False),
])
def test_identifies_resident_nexo_without_requesting_engine_or_inference(backend, loaded, display_only, eligible):
  client = SimpleNamespace(
    hello=lambda **kw: {'protocol': 2, 'backend': backend, 'nexo_display_only_v1': display_only},
    state=lambda **kw: {'loaded': loaded})
  result = check.inspect_peer(client, 'a' * 64)
  assert result['eligible'] is eligible
  assert not result['engine_contract_verified'] and not result['inference_verified'] and not result['physical_hud_verified']


@pytest.mark.parametrize('blocked', ['onroad', 'unknown', 'inference', 'ncm_display', 'bulk_display'])
def test_busy_or_unknown_owner_never_opens_connection(monkeypatch, tmp_path, blocked):
  marker = tmp_path / 'display'
  if blocked == 'ncm_display':
    marker.touch()
  monkeypatch.setattr(check, 'OPT_IN', marker)
  monkeypatch.setattr(check, 'enabled', lambda: blocked == 'inference')
  monkeypatch.setattr(check.JetlinkNcmTcpTransport, 'connect', lambda **kw: pytest.fail('must not connect'))
  values = {'IsOffroad': blocked != 'unknown', 'IsOnroad': blocked == 'onroad', 'NexoJetsonUsb': blocked == 'bulk_display'}
  with pytest.raises(RuntimeError):
    check.check(SimpleNamespace(get_bool=lambda k: values[k]))


@pytest.mark.parametrize('failure', ['hello', 'state', 'road_changed'])
def test_tcp_check_closes_on_failure_and_never_enables(tmp_path, monkeypatch, failure):
  model = tmp_path / 'model'
  model.write_bytes(b'unchanged model')
  monkeypatch.setattr(check, 'NATIVE_MODEL', model)
  checks, closed = [], []
  def idle(params):
    checks.append(True)
    if failure == 'road_changed' and len(checks) == 3:
      raise RuntimeError('road changed')
  monkeypatch.setattr(check, 'require_idle', idle)
  monkeypatch.setattr(check.JetlinkNcmTcpTransport, 'connect', lambda **kw: object())
  def fail(**kw):
    raise RuntimeError('peer unavailable')
  monkeypatch.setattr(check, 'JetlinkClient', lambda *a, **kw: SimpleNamespace(
    hello=fail if failure == 'hello' else lambda **kw: {}, state=fail,
    close=lambda: closed.append(True)))
  with pytest.raises(RuntimeError):
    check.check(None)
  assert closed == [True] and model.read_bytes() == b'unchanged model'
