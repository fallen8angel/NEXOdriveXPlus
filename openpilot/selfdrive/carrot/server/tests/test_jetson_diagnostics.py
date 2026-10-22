"""Read-only 7000 Jetson diagnostic tests. Hardware is never contacted."""
from openpilot.selfdrive.carrot.server.services import jetson_diagnostics as diag


def _usb():
  return {'udc_bound': False, 'controllers': [], 'ncm_interfaces': [], 'ncm_interface': None,
          'comma_ipv4': None, 'expected_comma_ipv4': '192.168.100.127',
          'expected_jetson_ipv4': '192.168.100.250', 'peer_route_via_usb': None}


def test_disabled_mode_does_not_claim_a_hardware_failure(monkeypatch):
  monkeypatch.setattr(diag, '_ncm_observation', _usb)
  monkeypatch.setattr(diag.ncm, 'transport_mode', lambda: 'usb')
  monkeypatch.setattr(diag, '_git_value', lambda *args: '')
  result = {'connected': False, 'status': {}, 'native_link': {}, 'stages': [], 'recent': {}}
  data = diag.build_report(result, {'enabled': False, 'usb_carrier': None}, True)
  assert data['mode']['jetlink_inference_enabled'] is False
  assert data['pipeline']['inference'] == '비활성'
  assert not any('UDC 바인딩' in note for note in data['observations'])
  assert data['read_only'] is True
  assert '전원' in diag.report_text(data)


def test_ncm_missing_function_and_link_are_reported_independently(monkeypatch):
  monkeypatch.setattr(diag, '_ncm_observation', _usb)
  monkeypatch.setattr(diag.ncm, 'transport_mode', lambda: 'ncm-tcp')
  monkeypatch.setattr(diag, '_git_value', lambda *args: '')
  data = diag.build_report({'connected': False, 'status': {}, 'stages': []},
                           {'enabled': True, 'usb_admin_up': False, 'usb_carrier': False}, True)
  assert any('UDC 바인딩' in note for note in data['observations'])
  assert any('DOWN' in note for note in data['observations'])
  assert data['jetlink']['peer_telemetry_fresh'] is False
