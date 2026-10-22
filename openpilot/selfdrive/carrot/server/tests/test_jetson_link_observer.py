"""Passive Jetson diagnostics: auto discovery, fault priority, remote trust, event persistence."""
from openpilot.selfdrive.carrot.server.services import jetson_diagnostics as diag
from openpilot.selfdrive.carrot.server.services.jetson_observer import LinkHistory, history_text


def sample():
  return {'usb_gadget': {'udc_bound': True, 'gadget_owners': ['g1'],
                         'ncm_interfaces': ['usb0'], 'ncm_interface': 'usb0',
                         'comma_ipv4': '192.168.100.127', 'peer_route_via_usb': True},
          'usb_link': {'usb_admin_up': True, 'usb_carrier': True},
          'jetlink': {'native_state': 'unavailable', 'peer_telemetry_fresh': False},
          'assessment': {'code': 'NCM_LINK_READY'}, 'network_recovery': {'state': 'ready'}}


def test_inference_off_does_not_hide_usb_readiness():
  s = sample()
  code, msg, _ = diag.classify(s['usb_gadget'], {'enabled': False, **s['usb_link']},
                                {'state': 'unavailable'}, {'connected': False}, 'ncm-tcp')
  assert code == 'NCM_LINK_READY'
  assert 'OFF' in msg


def test_usb_carrier_and_no_server_have_different_reasons():
  s = sample()
  code, _, advice = diag.classify(s['usb_gadget'], {'enabled': True, **s['usb_link']},
                                  {'state': 'unavailable'}, {'connected': False}, 'ncm-tcp')
  assert code == 'WAITING_FOR_JETSON'
  assert '5599' in advice
  s['usb_link']['usb_carrier'] = False
  code, _, _ = diag.classify(s['usb_gadget'], {'enabled': True, **s['usb_link']},
                             {'state': 'unavailable'}, {'connected': False}, 'ncm-tcp')
  assert code == 'USB_LINK_DOWN'


def test_wifi_beacon_does_not_verify_usb_hello():
  s = sample()
  code, _, _ = diag.classify(s['usb_gadget'], {'enabled': True, **s['usb_link']},
                             {'state': 'unavailable'}, {'connected': True}, 'ncm-tcp')
  assert code == 'PEER_BEACON_ONLY'


def test_waiting_does_not_claim_jetson_service(tmp_path, monkeypatch):
  s = sample()
  monkeypatch.setattr(diag, '_ncm_observation', lambda: s['usb_gadget'])
  monkeypatch.setattr(diag, 'network_recovery', lambda: {'state': 'ready'})
  monkeypatch.setattr(diag.ncm, 'transport_mode', lambda: 'ncm-tcp')
  report = diag.build_report({'connected': False, 'native_link': {}, 'status': {'service_active': True},
                              'health': {}, 'recent': {}, 'stages': []},
                             {'enabled': False, **s['usb_link']}, include_git=False)
  assert report['remote_boot_and_services']['reported_app_active'] is None
  assert report['remote_boot_and_services']['carrot_jetlink_service'] is None
  assert report['jetlink']['ncm_hello_verified'] is False


def test_history_debounced_deduplicated_and_reloaded(tmp_path):
  path = tmp_path / 'events.json'
  tracker = LinkHistory(path)
  sample_report = sample()
  assert not tracker.observe(sample_report)
  assert tracker.observe(sample_report)
  assert not tracker.observe(sample_report)
  sample_report['assessment']['code'] = 'WAITING_FOR_JETSON'
  assert not tracker.observe(sample_report)
  assert tracker.observe(sample_report)
  assert len(tracker.events) == 2
  assert len(LinkHistory(path).events) == 2
  assert 'NCM_LINK_READY' in history_text(tracker.export())


def test_history_does_not_store_raw_error_or_ssh_key(tmp_path):
  path = tmp_path / 'events.json'
  tracker = LinkHistory(path)
  sample_report = sample()
  sample_report['reported_error'] = 'password=secret-1234'
  tracker.observe(sample_report)
  tracker.observe(sample_report)
  assert 'secret-1234' not in path.read_text()
  assert len(tracker.export()['events']) == 1


def test_unknown_history_state_cannot_be_injected(tmp_path):
  path = tmp_path / 'events.json'
  tracker = LinkHistory(path)
  sample_report = sample()
  sample_report['assessment']['code'] = 'password=secret-1234'
  tracker.observe(sample_report)
  tracker.observe(sample_report)
  assert 'password=' not in path.read_text()
