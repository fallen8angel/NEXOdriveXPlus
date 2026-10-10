import json

import pytest

from openpilot.common import jetson_status as status


@pytest.fixture
def records(tmp_path, monkeypatch):
  marker = tmp_path / 'enabled'
  marker.touch()
  link = tmp_path / 'link.json'
  model = tmp_path / 'model.json'
  monkeypatch.setattr(status, 'JETLINK_ENABLED', marker)
  monkeypatch.setattr(status, 'JETLINK_STATUS', link)
  monkeypatch.setattr(status, 'JETLINK_MODEL_STATUS', model)
  return marker, link, model


def write(path, **values):
  path.write_text(json.dumps(values), encoding='utf-8')


def ready(path, **values):
  write(path, **{'state': 'ready', 'updated': 10., 'telemetry_updated': 10., 'sha256': 'nexo-model', **values})


def test_ready_and_active_badges_require_fresh_matching_native_model(records):
  _, link, model = records
  ready(link)
  badge = status.NativeJetlinkStatus()
  assert badge.update(10.1) == ('JETSON READY', 'ready')
  write(model, active=True, updated=10.2, sha256='nexo-model')
  assert badge.update(10.4) == ('JETSON', 'active')
  write(model, active=True, updated=10.5, sha256='other-model')
  assert badge.update(10.7) == ('JETSON READY', 'ready')
  assert badge.update(12.5) is None


@pytest.mark.parametrize('values', [
  {'state': 'connecting'}, {'state': 'retrying'}, {'state': 'stopped'},
  {'updated': 0}, {'telemetry_updated': 0}, {'telemetry_updated': 11},
  {'telemetry_updated': True}, {'telemetry_updated': float('nan')},
  {'telemetry_updated': 10 ** 500},
])
def test_admin_link_or_saved_ready_is_not_peer_connection(records, values):
  _, link, _ = records
  ready(link, **values)
  assert status.NativeJetlinkStatus().update(10.1) is None


def test_recent_inference_can_prove_connection_without_state_exchange(records):
  _, link, model = records
  ready(link, telemetry_updated=0, last_infer_monotonic=10)
  write(model, active=True, updated=10, sha256='nexo-model')
  assert status.NativeJetlinkStatus().update(10.1) == ('JETSON', 'active')


def test_bounded_read_disabled_marker_and_malformed_files_fail_quietly(records):
  marker, link, _ = records
  for raw in ('[]', '{', 'x' * 65537):
    link.write_text(raw, encoding='utf-8')
    assert status.NativeJetlinkStatus().update(10.1) is None
  ready(link)
  marker.unlink()
  assert status.NativeJetlinkStatus().update(10.1) is None


def test_cache_does_not_extend_connection_lease(records, monkeypatch):
  _, link, _ = records
  ready(link, updated=9.99, telemetry_updated=9.99)
  badge = status.NativeJetlinkStatus()
  assert badge.update(12.45)
  monkeypatch.setattr(status, '_record', lambda path: pytest.fail('cache reread'))
  assert badge.update(12.50) is None


def test_usb_and_wifi_leases_remain_independent():
  leases = status.JetsonConnectivity()
  leases.update({'magic': 'NEXO_JETSON_STATUS', 'transport': 'wifi', 'comma_tcp': True}, 10)
  leases.update({'magic': 'NEXO_JETSON_STATUS', 'transport': 'usb', 'usb_connected': False}, 11)
  assert leases.connected(11)
  assert not leases.connected(12.5)
