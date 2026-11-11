"""Unit tests for the opt-in LTE/USB gateway; no real network mutations."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import lte_share as gateway


def test_wan_disallows_wifi_and_unrecognized_interfaces(monkeypatch):
  for name in ('wlan0', 'usb0', 'eth0', 'tun0', 'bad dev', ''):
    monkeypatch.setattr(gateway, 'command', lambda args, dev=name: SimpleNamespace(stdout=json.dumps([{'dev': dev}])))
    with pytest.raises(gateway.GatewayError, match='Cellular default uplink'):
      gateway.cellular_wan()


def test_wan_accepts_cellular_only(monkeypatch):
  for dev in ('rmnet_data0', 'wwan0', 'ccmni0', 'pdp0'):
    monkeypatch.setattr(gateway, 'command', lambda args, name=dev: SimpleNamespace(stdout=json.dumps([{'dev': name}])))
    assert gateway.cellular_wan() == dev


def test_onroad_gate_fails_closed(monkeypatch):
  monkeypatch.setattr(gateway, 'offroad', lambda: False)
  with pytest.raises(gateway.GatewayError, match='Offroad'):
    gateway.enable()
  with pytest.raises(gateway.GatewayError, match='Offroad'):
    gateway.disable()


def test_rules_limit_forwarding_to_jetson_peer():
  entries = gateway.rules('usb0', 'rmnet_data0')
  assert len(entries) == 3
  assert all(gateway.COMMENT in spec for _, spec in entries)
  assert all(f'{gateway.JETSON}/32' in spec for _, spec in entries)
  assert not any('192.168.100.0/24' in spec for _, spec in entries)


def test_jetson_default_route_recognition_is_strict():
  route = {'dst': 'default', 'gateway': gateway.COMMA, 'dev': 'enxusb',
           'metric': gateway.METRIC, 'protocol': '187'}
  assert gateway.ours(route, 'enxusb')
  for name, value in [('gateway', '10.0.0.1'), ('dev', 'wlan0'), ('metric', 10), ('protocol', 'boot')]:
    different = dict(route)
    different[name] = value
    assert not gateway.ours(different, 'enxusb')


def test_retry_failure_does_not_remove_existing_gateway(monkeypatch, tmp_path):
  state = {'usb': 'usb0', 'wan': 'rmnet_data0', 'host': 'enxusb',
           'previous_forward': '0', 'boot': 'oldboot'}
  state_path = tmp_path / 'state.json'
  state_path.write_text(json.dumps(state))
  forward = tmp_path / 'ip_forward'
  forward.write_text('1')
  boot = tmp_path / 'boot_id'
  boot.write_text('oldboot')
  monkeypatch.setattr(gateway, 'STATE', state_path)
  monkeypatch.setattr(gateway, 'IP_FORWARD', forward)
  monkeypatch.setattr(gateway, 'BOOT', boot)
  monkeypatch.setattr(gateway, 'require_offroad', lambda: None)
  monkeypatch.setattr(gateway, 'find_comma_ncm', lambda: 'usb0')
  monkeypatch.setattr(gateway, 'cellular_wan', lambda: 'rmnet_data0')
  monkeypatch.setattr(gateway, 'jetson_ncm', lambda: 'enxusb')
  monkeypatch.setattr(gateway, 'jetson_routes', lambda: [{
    'dst': 'default', 'gateway': gateway.COMMA, 'dev': 'enxusb',
    'metric': gateway.METRIC, 'protocol': str(gateway.PROTOCOL),
  }])
  monkeypatch.setattr(gateway, 'present', lambda chain, spec: True)
  monkeypatch.setattr(gateway, 'update_jetson_route', lambda iface: (_ for _ in ()).throw(gateway.GatewayError('retry failure')))
  removed = []
  monkeypatch.setattr(gateway, 'remove_jetson_route', lambda iface: removed.append(iface))
  monkeypatch.setattr(gateway, 'iptables', lambda *args, **kwargs: removed.append(args))
  with pytest.raises(gateway.GatewayError, match='retry failure'):
    gateway.enable()
  assert removed == []
  assert json.loads(state_path.read_text()) == state


def test_status_without_opt_in_does_not_probe_or_modify_network(monkeypatch):
  monkeypatch.setattr(gateway, 'load', lambda: None)
  monkeypatch.setattr(gateway, 'jetson_routes', lambda: pytest.fail('unexpected SSH'))
  assert gateway.status()['enabled'] is False
