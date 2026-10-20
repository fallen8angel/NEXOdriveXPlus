"""Real gadget discovery/route validation with explicitly simulated Linux I/O."""
import copy
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.jetlink import network, ncm_transport as ncm
from openpilot.tools.jetson import protected_ncm as host
from openpilot.tools.jetson.tests import test_protected_ncm as host_tests

jetson_linux = host_tests.linux


@pytest.fixture
def comma(tmp_path, monkeypatch):
  net = tmp_path / 'comma-net'
  net.mkdir()
  gadget = tmp_path / 'gadgets/g1'
  function = gadget / 'configs/c.1/ncm.0'
  function.mkdir(parents=True)
  (gadget / 'UDC').write_text('a600000.dwc3')
  (gadget / 'configs/c.1/ffs.adb').mkdir()
  monkeypatch.setattr(ncm, 'NET_CLASS', net)
  monkeypatch.setattr(ncm, 'GADGET_ROOT', gadget.parent)
  monkeypatch.setattr(ncm, 'MODE_FILE', tmp_path / 'no-inference-transport')
  monkeypatch.setattr(network, 'enabled', lambda: pytest.fail('networking must not consult inference enablement'))
  monkeypatch.setattr(network.os, 'geteuid', lambda: 1000, raising=False)
  original_index = network.socket.if_nametoindex
  wifi = [{'dst': 'default', 'dev': 'wlan0', 'gateway': '192.168.45.1'},
          {'dst': '192.168.100.0/24', 'dev': 'wlan0'}]
  state = {'name': 'usb0', 'index': 9, 'up': False, 'addr': [], 'routes': copy.deepcopy(wifi),
           'empty_ipv4_rows': True, 'writes': [], 'reads': []}
  def attach(name='usb0', index=9, carrier='1'):
    state.update(name=name, index=index)
    (net / name).mkdir(exist_ok=True)
    (net / name / 'carrier').write_text(carrier)
    (function / 'ifname').write_text(name)
    (gadget / 'UDC').write_text('a600000.dwc3')
  attach()
  def ifindex(name):
    if name == state['name'] and (net / name).is_dir():
      return state['index']
    return original_index(name)
  monkeypatch.setattr(network.socket, 'if_nametoindex', ifindex)
  def run(command, **kwargs):
    assert kwargs.get('timeout') in (2, 3)
    args = command[2:] if command[:2] == ['sudo', '-n'] else command
    assert args[0] == 'ip'
    if args[1:3] == ['-j', 'link']:
      assert args[3:] == ['show', 'dev', state['name']]
      state['reads'].append(args)
      data = [{'ifname': state['name'], 'ifindex': state['index'], 'flags': ['UP'] if state['up'] else []}]
    elif args[1:4] == ['-j', '-4', 'addr']:
      state['reads'].append(args)
      data = [{'ifname': state['name'], 'ifindex': state['index'], 'flags': ['UP'] if state['up'] else [],
               'addr_info': state['addr']}]
      if not state['addr'] and state['empty_ipv4_rows']:
        data = []  # Actual AGNOS response reported with usb0 DOWN/no IPv4.
    elif args[1:5] == ['-j', '-4', 'route', 'show']:
      state['reads'].append(args)
      data = [r for r in state['routes'] if r['dst'] == ncm.JETSON_ADDR + '/32']
    elif args[1:5] == ['-j', '-4', 'route', 'get']:
      state['reads'].append(args)
      data = [r for r in state['routes'] if r['dst'] == ncm.JETSON_ADDR + '/32'] or wifi[:1]
    else:
      state['writes'].append(command)
      assert command[:2] == ['sudo', '-n']
      if args[1:3] == ['link', 'set']:
        assert args[3:] == ['dev', state['name'], 'up']
        state['up'] = True
      elif args[1:3] == ['addr', 'add']:
        assert args[3:] == [ncm.COMMA_ADDR + '/24', 'dev', state['name'], 'noprefixroute']
        assert not any(a.get('local') == ncm.COMMA_ADDR for a in state['addr']), 'duplicate address add'
        state['addr'].append({'local': ncm.COMMA_ADDR, 'prefixlen': 24})
      elif args[1:3] == ['route', 'add']:
        assert args[3:] == [ncm.JETSON_ADDR + '/32', 'dev', state['name'], 'src', ncm.COMMA_ADDR, 'proto', '186']
        state['routes'].append({'dst': ncm.JETSON_ADDR + '/32', 'dev': state['name'], 'prefsrc': ncm.COMMA_ADDR})
      else:
        pytest.fail(f'Unexpected mutation: {command}')
      data = []
    return subprocess.CompletedProcess(command, 0, json.dumps(data), '')
  monkeypatch.setattr(network.subprocess, 'run', run)
  state.update(attach=attach, net=net, gadget=gadget, function=function, wifi=wifi)
  return state


def test_boot_with_inference_off_provisions_once_and_preserves_wifi_g1_adb(comma):
  assert network.ensure_ncm_network() == ('ready', '')
  assert len(comma['writes']) == 3
  assert network.ensure_ncm_network() == ('ready', '')
  assert len(comma['writes']) == 3
  assert comma['routes'][:2] == comma['wifi']
  assert (comma['gadget'] / 'UDC').read_text() == 'a600000.dwc3'
  assert (comma['gadget'] / 'configs/c.1/ffs.adb').is_dir()
  assert not ncm.MODE_FILE.exists()


def test_existing_ip_and_route_are_not_duplicated(comma):
  comma['up'] = True
  comma['addr'] = [{'local': ncm.COMMA_ADDR, 'prefixlen': 24}, {'local': '192.168.100.128', 'prefixlen': 24}]
  assert network.ensure_ncm_network() == ('ready', '')
  assert len(comma['writes']) == 1 and 'route' in comma['writes'][0]
  assert len(comma['addr']) == 2
  assert network.ensure_ncm_network() == ('ready', '')
  assert len(comma['writes']) == 1


@pytest.mark.parametrize('up', [False, True])
@pytest.mark.parametrize('empty_rows', [False, True])
def test_addressless_interface_uses_independent_link_state(comma, up, empty_rows):
  comma.update(up=up, empty_ipv4_rows=empty_rows)
  assert network.ensure_ncm_network() == ('ready', '')
  assert len(comma['writes']) == (2 if up else 3)
  assert comma['addr'] == [{'local': ncm.COMMA_ADDR, 'prefixlen': 24}]
  assert ['ip', '-j', 'link', 'show', 'dev', 'usb0'] in comma['reads']
  assert network.ensure_ncm_network() == ('ready', '')
  assert len(comma['writes']) == (2 if up else 3)


@pytest.mark.parametrize('response', ['[]', '{}', 'not JSON', '[{}]',
                                     '[{"ifname":"wlan0","ifindex":9,"flags":[]}]',
                                     '[{"ifname":"usb0","ifindex":10,"flags":[]}]',
                                     '[{"ifname":"usb0","ifindex":9,"flags":null}]'])
def test_invalid_link_inspection_never_assigns_an_address(comma, monkeypatch, response):
  original = network.subprocess.run
  def run(command, **kwargs):
    if command[:3] == ['ip', '-j', 'link']:
      return subprocess.CompletedProcess(command, 0, response, '')
    return original(command, **kwargs)
  monkeypatch.setattr(network.subprocess, 'run', run)
  action, error = network.ensure_ncm_network()
  assert action == 'error' and 'stage=inspect link' in error
  assert comma['writes'] == []


@pytest.mark.parametrize('response,returncode', [('[]', 1), ('not JSON', 0), ('[{}]', 0),
                                               ('[{"ifname":"usb0","ifindex":9,"addr_info":null}]', 0)])
def test_failed_or_invalid_ipv4_inspection_is_not_treated_as_addressless(comma, monkeypatch, response, returncode):
  original = network.subprocess.run
  def run(command, **kwargs):
    if command[:4] == ['ip', '-j', '-4', 'addr']:
      return subprocess.CompletedProcess(command, returncode, response, 'inspection denied')
    return original(command, **kwargs)
  monkeypatch.setattr(network.subprocess, 'run', run)
  action, error = network.ensure_ncm_network()
  assert action == 'error' and 'stage=inspect IPv4 addresses' in error
  assert comma['writes'] == []


def test_empty_ipv4_query_does_not_hide_interface_replacement(comma, monkeypatch):
  original = ncm.ip_json
  def query(*args):
    result = original(*args)
    if args[0] == 'addr':
      assert result == []
      comma['index'] += 1
    return result
  monkeypatch.setattr(ncm, 'ip_json', query)
  assert network.ensure_ncm_network()[0] == 'error'
  assert comma['writes'] == []


def test_command_error_reports_device_stage_and_stderr_to_worker_log(comma, monkeypatch):
  original = network.subprocess.run
  def run(command, **kwargs):
    if command[:2] == ['sudo', '-n']:
      raise subprocess.CalledProcessError(2, command, stderr='RTNETLINK answers: Operation not permitted')
    return original(command, **kwargs)
  monkeypatch.setattr(network.subprocess, 'run', run)
  logs = []
  logger = SimpleNamespace(info=lambda *a: logs.append(a), warning=lambda *a: logs.append(a))
  monkeypatch.setitem(sys.modules, 'openpilot.common.swaglog', SimpleNamespace(cloudlog=logger))
  class Finished(Exception):
    pass
  def sleep(seconds):
    raise Finished
  monkeypatch.setattr(network.time, 'sleep', sleep)
  with pytest.raises(Finished):
    network.main()
  message = logs[0][1]
  assert 'usb0 ifindex=9 stage=ip link set dev usb0 up' in message
  assert 'CalledProcessError' in message and 'Operation not permitted' in message
  assert comma['writes'] == [] and comma['addr'] == []


@pytest.mark.parametrize('loss', ['addr', 'route', 'link', 'all'])
def test_address_route_and_admin_loss_are_repaired_without_restart(comma, loss):
  assert network.ensure_ncm_network()[0] == 'ready'
  if loss in ('addr', 'all'):
    comma['addr'] = []
  if loss in ('route', 'all'):
    comma['routes'] = copy.deepcopy(comma['wifi'])
  if loss in ('link', 'all'):
    comma['up'] = False
  comma['writes'].clear()
  assert network.ensure_ncm_network()[0] == 'ready'
  assert len(comma['writes']) == (3 if loss == 'all' else 1)
  assert comma['routes'][:2] == comma['wifi']


def test_usb_delay_detach_rename_and_carrier_reconnect(comma):
  (comma['gadget'] / 'UDC').write_text('')
  assert network.ensure_ncm_network()[0] == 'waiting_ncm'
  assert comma['writes'] == []
  comma['attach'](carrier='0')
  assert network.ensure_ncm_network() == ('waiting_usb', '')
  assert len(comma['writes']) == 3  # Provision even while the Jetson is booting.
  (comma['net'] / 'usb0/carrier').write_text('1')
  assert network.ensure_ncm_network()[0] == 'ready'
  (comma['net'] / 'usb0/carrier').unlink()
  (comma['net'] / 'usb0').rmdir()
  assert network.ensure_ncm_network()[0] == 'waiting_ncm'
  comma.update(addr=[], routes=copy.deepcopy(comma['wifi']), up=False)
  comma['attach']('usb1', 17)
  assert network.ensure_ncm_network()[0] == 'ready'
  assert all('usb1' in c for c in comma['writes'][-3:])


@pytest.mark.parametrize('bad', ['unbound', 'non_ncm', 'ambiguous', 'prefix', 'foreign_route', 'gateway', 'multipath'])
def test_wrong_device_or_conflicting_state_never_mutates(comma, bad):
  if bad == 'unbound':
    (comma['gadget'] / 'UDC').write_text('')
  elif bad == 'non_ncm':
    (comma['function'] / 'ifname').unlink()
    comma['function'].rmdir()
    (comma['gadget'] / 'configs/c.1/ecm.0').mkdir()
  elif bad == 'ambiguous':
    other = comma['gadget'] / 'configs/c.1/ncm.1'
    other.mkdir()
    (other / 'ifname').write_text('wlan0')
    (comma['net'] / 'wlan0').mkdir()
  elif bad == 'prefix':
    comma['addr'] = [{'local': ncm.COMMA_ADDR, 'prefixlen': 16}]
  else:
    route = {'dst': ncm.JETSON_ADDR + '/32', 'dev': 'usb0'}
    if bad == 'foreign_route':
      route['dev'] = 'wlan0'
    elif bad == 'gateway':
      route['gateway'] = '192.168.45.1'
    else:
      route['multipath'] = [{'dev': 'wlan0'}]
    comma['routes'].append(route)
  assert network.ensure_ncm_network()[0] in ('waiting_ncm', 'error')
  assert comma['writes'] == []


def test_ifindex_replaced_before_mutation_is_retried_without_wrong_assignment(comma, monkeypatch):
  original = ncm.ip_json
  def query(*args):
    value = original(*args)
    if args[:2] == ('route', 'show'):
      comma['index'] += 1
    return value
  monkeypatch.setattr(ncm, 'ip_json', query)
  assert network.ensure_ncm_network()[0] == 'error'
  assert comma['writes'] == []
  monkeypatch.setattr(ncm, 'ip_json', original)
  assert network.ensure_ncm_network()[0] == 'ready'


@pytest.mark.parametrize('failure', [PermissionError('sudo unavailable'), subprocess.TimeoutExpired('ip', 2),
                                  subprocess.CalledProcessError(2, 'ip')])
def test_failed_command_does_not_crash_worker_and_retries(comma, monkeypatch, failure):
  original = network.subprocess.run
  def run(command, **kwargs):
    if command[:2] == ['sudo', '-n']:
      raise failure
    return original(command, **kwargs)
  monkeypatch.setattr(network.subprocess, 'run', run)
  assert network.ensure_ncm_network()[0] == 'error'
  assert comma['addr'] == []
  monkeypatch.setattr(network.subprocess, 'run', original)
  assert network.ensure_ncm_network()[0] == 'ready'


def test_actual_worker_loop_waits_for_late_usb_and_recovers_lost_address(comma, monkeypatch):
  logs, ticks = [], []
  logger = SimpleNamespace(info=lambda *a: logs.append(a), warning=lambda *a: logs.append(a))
  monkeypatch.setitem(sys.modules, 'openpilot.common.swaglog', SimpleNamespace(cloudlog=logger))
  (comma['gadget'] / 'UDC').write_text('')
  class Finished(Exception):
    pass
  def sleep(seconds):
    assert seconds == 2
    ticks.append(seconds)
    if len(ticks) == 1:
      assert comma['writes'] == []
      comma['attach']()
    elif len(ticks) == 2:
      assert comma['addr']
      comma['addr'] = []
    else:
      assert comma['addr']  # Same process restored the lost address.
      raise Finished
  monkeypatch.setattr(network.time, 'sleep', sleep)
  with pytest.raises(Finished):
    network.main()
  assert any('waiting_ncm' in row for row in logs)
  assert any('ready' in row for row in logs)


@pytest.mark.parametrize('reboot', ['comma', 'jetson', 'both'])
def test_paired_boot_recovery_reuses_unmodified_jetson_worker(jetson_linux, comma, reboot):
  config = host.configuration('enx36770f430ea2', 'f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5')
  assert host.network_once(config)['state'] == 'ready'
  assert network.ensure_ncm_network()[0] == 'ready'
  if reboot in ('comma', 'both'):
    comma.update(addr=[], routes=copy.deepcopy(comma['wifi']), up=False, index=comma['index'] + 1)
  if reboot in ('jetson', 'both'):
    entry = jetson_linux['names'][config['interface']]
    entry.update(addr=[], up=False, managed='yes', index=entry['index'] + 1)
    jetson_linux['routes'] = []
  # USB carrier drops while either end is rebooting. Neither side waits for
  # the other IP to exist before restoring its own address.
  comma_carrier = comma['net'] / comma['name'] / 'carrier'
  host_carrier = host.NET / config['interface'] / 'carrier'
  comma_carrier.write_text('0')
  host_carrier.write_text('0')
  assert network.ensure_ncm_network()[0] == 'waiting_usb'
  assert host.network_once(config)['state'] == 'waiting_usb'
  comma_carrier.write_text('1')
  host_carrier.write_text('1')
  assert network.ensure_ncm_network()[0] == 'ready'
  assert host.network_once(config)['state'] == 'ready'
  assert comma['routes'][:2] == comma['wifi']
