"""Unit tests for the opt-in LTE/USB gateway; no real network mutations."""
import json
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


def test_not_configured_gateway_reports_safe_no_wifi_alternative(monkeypatch):
  monkeypatch.setattr(gateway, 'offroad', lambda: False)
  monkeypatch.setattr(gateway, 'load', lambda: None)
  monkeypatch.setattr(gateway, 'sharing_uplink', lambda: {'wan': 'ppp0', 'route_mode': 'existing_default'})
  monkeypatch.setattr(gateway, 'find_comma_ncm', lambda: 'usb0')
  monkeypatch.setattr(gateway, 'select_firewall_backend', lambda: 'iptables-legacy')
  result = gateway.status()
  assert result['configured'] is False and result['can_change'] is False
  assert result['jetson_wifi_required_for_comma_hud_download'] is False
  assert 'P단 Onroad' in result['next_action']
  assert result['active_cellular_wan'] == 'ppp0'
  assert result['enabled'] is False and result['operational'] is False


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


def test_jetson_usb_route_without_optional_source_field_is_validated_by_real_interface(monkeypatch):
  calls = []
  def remote(*args):
    calls.append(args)
    if args[:5] == ('ip', '-j', '-4', 'route', 'get'):
      return SimpleNamespace(stdout=json.dumps([{
        'dst': gateway.COMMA, 'dev': 'enx3ea33b63f8a7', 'flags': [],
      }]))
    if args[:2] == ('readlink', '-f'):
      return SimpleNamespace(stdout='/sys/bus/usb/drivers/cdc_ncm\n')
    if args[:5] == ('ip', '-j', '-4', 'addr', 'show'):
      return SimpleNamespace(stdout=json.dumps([{
        'ifname': 'enx3ea33b63f8a7',
        'addr_info': [{'local': gateway.JETSON, 'prefixlen': 24}],
      }]))
    if args[:1] == ('cat',):
      return SimpleNamespace(stdout='1\n')
    if args == ('sudo', '-n', 'true'):
      return SimpleNamespace(stdout='')
    pytest.fail(f'unexpected read-only probe: {args}')
  monkeypatch.setattr(gateway, 'remote', remote)
  assert gateway.jetson_ncm() == 'enx3ea33b63f8a7'
  assert calls[-1] == ('sudo', '-n', 'true')


@pytest.mark.parametrize('route', [
  {'dst': gateway.COMMA, 'dev': 'enx3ea33b63f8a7', 'gateway': '192.168.1.1'},
  {'dst': gateway.COMMA, 'dev': 'enx3ea33b63f8a7', 'via': '192.168.1.1'},
  {'dst': gateway.COMMA, 'dev': 'enx3ea33b63f8a7', 'src': '192.168.100.77'},
  {'dst': gateway.COMMA, 'dev': 'enx3ea33b63f8a7', 'prefsrc': '192.168.100.77'},
  {'dst': gateway.COMMA, 'dev': 'enx3ea33b63f8a7', 'type': 'local'},
  {'dst': '192.168.100.126', 'dev': 'enx3ea33b63f8a7'},
])
def test_jetson_usb_route_rejects_indirect_wrong_source_or_wrong_target(monkeypatch, route):
  calls = []
  def remote(*args):
    calls.append(args)
    assert args[:5] == ('ip', '-j', '-4', 'route', 'get')
    return SimpleNamespace(stdout=json.dumps([route]))
  monkeypatch.setattr(gateway, 'remote', remote)
  with pytest.raises(gateway.GatewayError, match='not direct USB-NCM'):
    gateway.jetson_ncm()
  assert len(calls) == 1  # Reject before any privileged operations.


def test_jetson_default_route_recognition_is_strict():
  route = {'dst': 'default', 'gateway': gateway.COMMA, 'dev': 'enxusb',
           'metric': gateway.METRIC, 'protocol': '187'}
  assert gateway.ours(route, 'enxusb')
  for name, value in [('gateway', '10.0.0.1'), ('dev', 'wlan0'), ('metric', 10), ('protocol', 'boot')]:
    different = dict(route)
    different[name] = value
    assert not gateway.ours(different, 'enxusb')


@pytest.mark.parametrize('protocol', [187, '187', 'isis'])
def test_iproute2_protocol_name_does_not_hide_managed_route(protocol):
  route = {'dst': 'default', 'gateway': gateway.COMMA, 'dev': 'enxusb',
           'metric': gateway.METRIC, 'protocol': protocol}
  assert gateway.ours(route, 'enxusb')
  assert not gateway.ours({**route, 'metric': 1}, 'enxusb')


def test_route_query_forces_numeric_protocol_and_named_conflict_is_preserved(monkeypatch):
  calls = []
  monkeypatch.setattr(gateway, 'remote', lambda *args: calls.append(args) or SimpleNamespace(stdout='[]'))
  assert gateway.jetson_routes() == []
  assert calls == [('ip', '-N', '-j', '-4', 'route', 'show', 'default')]
  monkeypatch.setattr(gateway, 'jetson_routes', lambda: [{'protocol': 'isis', 'dev': 'wlan0'}])
  with pytest.raises(gateway.GatewayError, match='conflicting'):
    gateway.update_jetson_route('enxusb')
  assert len(calls) == 1


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
  monkeypatch.setattr(gateway, 'offroad', lambda: True)
  monkeypatch.setattr(gateway, 'select_firewall_backend', lambda *a: 'iptables')
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

def test_unconfigured_gateway_explains_comma_download_not_jetson_wifi(monkeypatch):
  monkeypatch.setattr(gateway, 'load', lambda: None)
  value = gateway.status()
  assert value['lte_share_opt_in_only']
  assert 'Jetson Wi-Fi is not required' in value['update_note']
  assert not value['enabled']

def test_status_reports_offroad_gate_without_mutation(monkeypatch):
  monkeypatch.setattr(gateway, 'load', lambda: None)
  monkeypatch.setattr(gateway, 'offroad', lambda: False)
  value = gateway.status()
  assert value['offroad'] is False and value['can_change'] is False
  assert value['enabled'] is False
  monkeypatch.setattr(gateway, 'offroad', lambda: True)
  value = gateway.status()
  assert value['can_change'] is True and value['enabled'] is False


def test_status_distinguishes_gateway_rules_from_current_cellular_route(monkeypatch, tmp_path):
  monkeypatch.setattr(gateway, 'load', lambda: {'usb': 'usb0', 'wan': 'rmnet_data0', 'host': 'enxusb'})
  monkeypatch.setattr(gateway, 'present', lambda chain, spec: True)
  monkeypatch.setattr(gateway, 'jetson_routes', lambda: [{'dst': 'default', 'gateway': gateway.COMMA,
    'dev': 'enxusb', 'metric': gateway.METRIC, 'protocol': str(gateway.PROTOCOL)}])
  monkeypatch.setattr(gateway, 'cellular_wan', lambda: 'wwan0')
  forward = tmp_path / 'forward'
  forward.write_text('1')
  monkeypatch.setattr(gateway, 'IP_FORWARD', forward)
  report = gateway.status()
  assert report['enabled'] is True  # Rules installed, but not a usable LTE path.
  assert report['operational'] is False
  assert report['active_cellular_wan'] == 'wwan0'
  assert report['wan_route_matches_config'] is False
  assert report['internet_dns_verified'] is False


def test_wifi_default_change_invalidates_cached_lte_success_even_with_same_ppp_interface(monkeypatch, tmp_path):
  monkeypatch.setattr(gateway, 'load', lambda: {'usb': 'usb0', 'wan': 'ppp0', 'host': 'enxusb'})
  monkeypatch.setattr(gateway, 'present', lambda *args: True)
  monkeypatch.setattr(gateway, 'jetson_routes', lambda: [{'dst': 'default', 'gateway': gateway.COMMA,
    'dev': 'enxusb', 'metric': gateway.METRIC, 'protocol': 187}])
  monkeypatch.setattr(gateway, 'sharing_uplink', lambda: {'wan': 'ppp0', 'route_mode': 'cellular_policy', 'eligible': True})
  monkeypatch.setattr(gateway, 'probe_record', lambda state: {'internet_dns_verified': True})
  forward = tmp_path / 'forward'
  forward.write_text('1')
  monkeypatch.setattr(gateway, 'IP_FORWARD', forward)
  value = gateway.status()
  assert value['active_cellular_wan'] == 'ppp0'  # Same interface alone does not prove same path.
  assert value['wan_route_matches_config'] is False and value['internet_dns_verified'] is False
  assert value['operational'] is False
  assert 'CELLULAR_ROUTE_MODE_CHANGED' in [f['code'] for f in value['failures']]


def test_diagnostic_reports_independent_failures_without_changes(monkeypatch, tmp_path):
  monkeypatch.setattr(gateway, 'load', lambda: {'usb': 'usb0', 'wan': 'rmnet_data0', 'host': 'enxusb'})
  def fail(*args, **kwargs):
    raise gateway.GatewayError('SSH peer unavailable')
  monkeypatch.setattr(gateway, 'present', fail)
  monkeypatch.setattr(gateway, 'jetson_routes', fail)
  monkeypatch.setattr(gateway, 'cellular_wan', fail)
  monkeypatch.setattr(gateway, 'IP_FORWARD', tmp_path / 'missing')
  monkeypatch.setattr(gateway, 'enable', lambda: pytest.fail('unexpected enable'))
  monkeypatch.setattr(gateway, 'disable', lambda: pytest.fail('unexpected disable'))
  value = gateway.status()
  assert value['read_only'] is True and value['enabled'] is False
  assert {item['code'] for item in value['failures']} == {
    'FIREWALL_CHECK_FAILED', 'JETSON_SSH_OR_ROUTE_FAILED', 'IP_FORWARD_UNAVAILABLE', 'CELLULAR_UPLINK_UNVERIFIED'}
  assert value['internet_dns_verified'] is False


def test_firewall_permission_failure_is_not_a_missing_rule(monkeypatch):
  monkeypatch.setattr(gateway, 'iptables', lambda *a, **k: SimpleNamespace(returncode=2, stderr='permission denied'))
  with pytest.raises(gateway.GatewayError, match='permission denied'):
    gateway.present(['FORWARD'], [])


def test_corrupt_gateway_state_still_returns_read_only_diagnostics(monkeypatch):
  def invalid():
    raise gateway.GatewayError('Invalid state file')
  monkeypatch.setattr(gateway, 'load', invalid)
  value = gateway.status()
  assert value['state'] == 'unknown' and value['configured'] is None
  assert value['failures'][0]['code'] == 'SAVED_STATE_INVALID'


@pytest.mark.parametrize('link,allowed', [
  ({'link_type': 'ppp', 'flags': ['POINTOPOINT', 'UP']}, True),
  ({'link_type': 'ether', 'flags': ['UP']}, False),
  ({'link_type': 'ppp', 'flags': ['POINTOPOINT']}, False),
])
def test_comma4_modem_ppp0_requires_a_live_ppp_route(monkeypatch, link, allowed):
  def run(args, **kwargs):
    return SimpleNamespace(stdout=json.dumps([{'dev': 'ppp0'}] if 'route' in args else [link]))
  monkeypatch.setattr(gateway, 'command', run)
  if allowed:
    assert gateway.cellular_wan() == 'ppp0'
  else:
    with pytest.raises(gateway.GatewayError, match='PPP'):
      gateway.cellular_wan()


def test_unconfigured_status_discovers_local_prerequisites_without_ssh(monkeypatch):
  monkeypatch.setattr(gateway, 'load', lambda: None)
  monkeypatch.setattr(gateway, 'cellular_wan', lambda: 'ppp0')
  monkeypatch.setattr(gateway, 'find_comma_ncm', lambda: 'usb0')
  monkeypatch.setattr(gateway, 'select_firewall_backend', lambda: 'iptables-legacy')
  monkeypatch.setattr(gateway, 'remote', lambda *a, **kw: pytest.fail('unconfigured status used SSH'))
  value = gateway.status()
  assert value['active_cellular_wan'] == 'ppp0'
  assert value['firewall_backend'] == 'iptables-legacy'
  assert not value['configured'] and not value['enabled']


@pytest.fixture
def network(monkeypatch, tmp_path):
  """Exercise real transaction/rule selection; only command/device I/O is fake."""
  monkeypatch.setattr(gateway, 'STATE', tmp_path / 'state.json')
  monkeypatch.setattr(gateway, 'PROBE', tmp_path / 'probe.json')
  monkeypatch.setattr(gateway, 'IP_FORWARD', tmp_path / 'forward')
  gateway.IP_FORWARD.write_text('0')
  monkeypatch.setattr(gateway, 'BOOT', tmp_path / 'boot')
  gateway.BOOT.write_text('test-boot')
  net = {'offroad': True, 'rules': {('foreign',)}, 'routes': [{'dev': 'wlan0', 'gateway': '10.0.0.1'}],
         'mutations': [], 'lost_reply': False, 'cleanup_ok': True, 'enter_onroad': False}
  monkeypatch.setattr(gateway, 'offroad', lambda: net['offroad'])
  monkeypatch.setattr(gateway, 'find_comma_ncm', lambda: 'usb0')
  monkeypatch.setattr(gateway, 'cellular_wan', lambda: 'ppp0')
  monkeypatch.setattr(gateway, 'jetson_ncm', lambda: 'enxusb')
  monkeypatch.setattr(gateway, 'jetson_routes', lambda: list(net['routes']))
  def command(args, **kw):
    assert args[:2] == ['sudo', '-n']
    if args[2] == 'sysctl':
      gateway.require_offroad()
      net['mutations'].append(tuple(args))
      gateway.IP_FORWARD.write_text(args[-1][-1])
      if net['enter_onroad']:
        net['offroad'] = False
      return SimpleNamespace(returncode=0, stdout='', stderr='')
    backend = args[2]
    assert args[3:5] == ['-w', '2']
    if '-S' in args:
      return SimpleNamespace(returncode=int(backend != 'iptables-legacy'), stderr='nft unsupported')
    action = next(a for a in args if a in ('-C', '-I', '-D'))
    index = args.index(action)
    key = (backend, *args[5:index], *args[index + 1:])
    if action == '-C':
      return SimpleNamespace(returncode=int(key not in net['rules']), stderr='')
    gateway.require_offroad()
    net['mutations'].append(tuple(args))
    if action == '-I':
      net['rules'].add(key)
    else:
      net['rules'].remove(key)
    return SimpleNamespace(returncode=0, stdout='', stderr='')
  monkeypatch.setattr(gateway, 'command', command)
  def route_add(iface):
    gateway.require_offroad()
    net['mutations'].append(('route-add', iface))
    net['routes'].append({'dst': 'default', 'gateway': gateway.COMMA, 'dev': iface,
                          'metric': gateway.METRIC, 'protocol': str(gateway.PROTOCOL)})
    if net['lost_reply']:
      raise gateway.GatewayError('SSH reply lost after route installation')
  def route_remove(iface):
    gateway.require_offroad()
    if not net['cleanup_ok']:
      return False
    net['routes'][:] = [r for r in net['routes'] if not gateway.ours(r, iface)]
    return True
  monkeypatch.setattr(gateway, 'update_jetson_route', route_add)
  monkeypatch.setattr(gateway, 'remove_jetson_route', route_remove)
  return net


def test_legacy_fallback_enable_verifies_and_disable_preserves_foreign_rules_wifi(network):
  assert 'enabled' in gateway.enable()
  assert gateway.load()['firewall_backend'] == 'iptables-legacy'
  assert gateway.IP_FORWARD.read_text() == '1'
  assert gateway.status()['enabled'] is True
  assert gateway.status()['operational'] is True
  assert len(network['rules']) == 4
  assert 'disabled' in gateway.disable()
  assert network['rules'] == {('foreign',)}
  assert network['routes'] == [{'dev': 'wlan0', 'gateway': '10.0.0.1'}]
  assert gateway.IP_FORWARD.read_text() == '0'
  assert gateway.load() is None


def test_incomplete_rollback_retains_journal_until_explicit_cleanup(network):
  network.update(lost_reply=True, cleanup_ok=False)
  with pytest.raises(gateway.GatewayError, match='reply lost'):
    gateway.enable()
  assert gateway.load()['recovery_pending'] is True
  assert gateway.load()['rollback_errors'] == ['Jetson route cleanup unconfirmed']
  assert 'Retry' in gateway.disable()
  assert gateway.load() is not None
  network['cleanup_ok'] = True
  gateway.disable()
  assert gateway.load() is None


def test_mid_transaction_onroad_never_changes_network_during_rollback(network):
  network['enter_onroad'] = True
  with pytest.raises(gateway.GatewayError, match='Offroad'):
    gateway.enable()
  assert gateway.load()['recovery_pending'] is True
  assert gateway.IP_FORWARD.read_text() == '1'
  assert len(network['mutations']) == 4  # Three scoped rules and forwarding, then NO cleanup writes.
  assert len(network['rules']) == 4
  with pytest.raises(gateway.GatewayError, match='Offroad'):
    gateway.disable()
  assert len(network['mutations']) == 4
  network.update(offroad=True, enter_onroad=False)
  gateway.disable()
  assert network['rules'] == {('foreign',)}


def test_no_silent_backend_switch_for_previously_installed_rules(network, monkeypatch):
  gateway.save({'usb': 'usb0', 'wan': 'ppp0', 'host': 'enxusb', 'firewall_backend': 'iptables'})
  with pytest.raises(gateway.GatewayError, match='No operational'):
    gateway.enable()
  assert not network['mutations']


def test_readback_failure_is_not_reported_as_enabled(network, monkeypatch):
  add = gateway.update_jetson_route
  def vanish(iface):
    add(iface)
    gateway.IP_FORWARD.write_text('0')
  monkeypatch.setattr(gateway, 'update_jetson_route', vanish)
  with pytest.raises(gateway.GatewayError, match='verification failed'):
    gateway.enable()
  assert gateway.load() is None
  assert network['rules'] == {('foreign',)}


@pytest.mark.parametrize('failed_action', ['-I', 'sysctl'])
def test_lost_local_reply_after_mutation_is_rolled_back(network, monkeypatch, failed_action):
  command, failed = gateway.command, [False]
  def lost_reply(args, **kwargs):
    result = command(args, **kwargs)
    if failed_action in args and not failed[0]:
      failed[0] = True
      raise gateway.GatewayError('simulated timeout after kernel accepted the change')
    return result
  monkeypatch.setattr(gateway, 'command', lost_reply)
  with pytest.raises(gateway.GatewayError, match='timeout'):
    gateway.enable()
  assert failed[0] and gateway.load() is None
  assert network['rules'] == {('foreign',)}
  assert gateway.IP_FORWARD.read_text() == '0'


def test_policy_failure_rolls_back_only_new_rules_and_preserves_wifi(network, monkeypatch):
  from openpilot.tools.jetson import lte_uplink
  monkeypatch.setattr(gateway, 'sharing_uplink', lambda: {'wan': 'ppp0', 'route_mode': 'cellular_policy'})
  monkeypatch.setattr(lte_uplink, 'preflight', lambda *a: [])
  mutations = []
  def install(command, gate, usb, added):
    gate()
    added.append(18701)
    mutations.append(('guard-added', usb))
    raise gateway.GatewayError('lost policy reply')
  def remove(command, gate, usb, added):
    gate()
    mutations.append(('remove-new-only', usb, list(added)))
  monkeypatch.setattr(lte_uplink, 'install', install)
  monkeypatch.setattr(lte_uplink, 'remove', remove)
  with pytest.raises(gateway.GatewayError, match='lost policy reply'):
    gateway.enable()
  assert mutations == [('guard-added', 'usb0'), ('remove-new-only', 'usb0', [18701])]
  assert network['rules'] == {('foreign',)}
  assert network['routes'] == [{'dev': 'wlan0', 'gateway': '10.0.0.1'}]
  assert not gateway.STATE.exists()
