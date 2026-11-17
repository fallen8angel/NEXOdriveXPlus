"""LTE policy requires fresh independent modem/PPP evidence and owns only its rules."""
import json
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import lte_uplink as policy


@pytest.fixture
def kernel(monkeypatch, tmp_path):
  modem = tmp_path / 'modem'
  monkeypatch.setattr(policy, 'MODEM', modem)
  monkeypatch.setattr(policy.time, 'monotonic', lambda: 100.)
  modem.write_text(json.dumps({'connected': True, 'seconds_since_boot': 99., 'ip_address': '10.1.0.2',
                               'network_type': 'LTE', 'imei': 'secret-must-not-export'}))
  state = {'rules': [{'priority': 0, 'table': 255}, {'priority': 32766, 'table': 254}],
           'mutations': [], 'gate': True, 'route': True, 'peer': '10.1.0.1', 'up': True, 'reply_lost': False}
  def gate():
    if not state['gate']:
      raise RuntimeError('Offroad required')
  def command(args, **kw):
    if args[:2] == ['sudo', '-n']:
      gate()
      state['mutations'].append(args)
      p = int(args[args.index('pref') + 1])
      row = {'priority': p, 'src': policy.JETSON, 'iif': 'usb0', 'protocol': 187}
      row.update({'table': 1000} if p == policy.PRIORITIES[0] else {'action': 'unreachable'})
      if args[5] == 'add':
        state['rules'].append(row)
        if state['reply_lost']:
          raise RuntimeError('reply lost after mutation')
      else:
        state['rules'].remove(row)
      return SimpleNamespace(stdout='')
    if 'rule' in args:
      result = state['rules']
    elif 'link' in args:
      result = [{'link_type': 'ppp', 'flags': ['UP', 'POINTOPOINT'] if state['up'] else []}]
    elif 'addr' in args:
      result = [{'ifname': 'ppp0', 'addr_info': [
        {'family': 'inet', 'local': state.get('local', '10.1.0.2'),
         state.get('peer_key', 'peer'): state['peer']},
      ]}]
    elif 'show' in args and 'table' in args:
      result = [{'dst': 'default', 'gateway': '10.1.0.1', 'dev': 'ppp0'}] if state['route'] else []
    elif 'show' in args:
      result = [{'dev': 'wlan0', 'gateway': '192.0.2.1'}]
    elif 'from' in args:
      result = [{'dev': 'ppp0', 'table': 1000}] if state['route'] else []
    else:
      result = [{'dev': 'wlan0'}]
    return SimpleNamespace(stdout=json.dumps(result))
  return state, command, gate, modem


def test_wifi_default_uses_only_proven_existing_lte_table(kernel):
  state, command, gate, _ = kernel
  result = policy.inspect(command)
  assert result['eligible'] is True and result['default_interface'] == 'wlan0'
  assert result['failed_step'] is None
  assert result['policy_route']['gateway_matches_peer'] is True
  assert 'secret' not in json.dumps(result)
  assert state['mutations'] == []
  added = []
  policy.install(command, gate, 'usb0', added)
  assert added == [18701, 18700]  # Reject Wi-Fi fall-through before lookup.
  assert policy.verified(command, 'usb0') is True
  state['route'] = False
  assert policy.verified(command, 'usb0') is False
  policy.remove(command, gate, 'usb0')
  assert [r['priority'] for r in state['rules']] == [0, 32766]
  assert [int(a[a.index('pref') + 1]) for a in state['mutations']] == [18701, 18700, 18700, 18701]
  assert all('default' not in a and 'sysctl' not in a for a in state['mutations'])


def test_live_nexo_ppp_json_address_key_matches_modem_and_existing_policy_table(kernel):
  """Real iproute2 JSON calls the PPP peer 'address', not 'peer'."""
  state, command, _, modem = kernel
  modem.write_text(json.dumps({
    'connected': True, 'seconds_since_boot': 99., 'ip_address': '10.58.167.83',
    'network_type': 'lte',
  }))
  state.update(local='10.58.167.83', peer_key='address', peer='10.64.64.64/32')
  def actual_route(args, **kwargs):
    result = command(args, **kwargs)
    if 'show' in args and 'table' in args:
      return SimpleNamespace(stdout=json.dumps([{
        'dst': 'default', 'gateway': '10.64.64.64', 'dev': 'ppp0',
      }]))
    return result
  report = policy.inspect(actual_route)
  assert report['eligible'] is True
  assert report['policy_route']['gateway_matches_peer'] is True
  assert report['policy_route']['ppp_address_matches_modem'] is True
  assert state['mutations'] == []


@pytest.mark.parametrize('detail', ['missing_remote', 'wrong_modem_local', 'bad_peer', 'same_as_local'])
def test_address_key_never_trusts_invalid_ppp_endpoint(kernel, detail):
  state, command, _, modem = kernel
  state['peer_key'] = 'address'
  if detail == 'missing_remote':
    state['peer'] = ''
  elif detail == 'wrong_modem_local':
    state['local'] = '10.99.0.2'
  elif detail == 'bad_peer':
    state['peer'] = 'not-an-ip-address'
  else:
    state['peer'] = '10.1.0.2'
  report = policy.inspect(command)
  assert report['eligible'] is False
  assert report['failed_step'] == 'ppp_policy_table'
  assert state['mutations'] == []


def test_conflicting_peer_json_aliases_are_rejected():
  addresses = [{'ifname': 'ppp0', 'addr_info': [
    {'family': 'inet', 'local': '10.1.0.2',
     'peer': '10.1.0.1', 'address': '10.1.0.9'},
  ]}]
  assert policy.ppp_peer_addresses(addresses, '10.1.0.2') == set()


@pytest.mark.parametrize('failure', ['stale', 'disconnected', 'missing', 'peer', 'route', 'link'])
def test_incomplete_cellular_evidence_never_authorizes_sharing(kernel, failure):
  state, command, _, modem = kernel
  value = json.loads(modem.read_text())
  if failure == 'stale':
    value['seconds_since_boot'] = 10
  elif failure == 'disconnected':
    value['connected'] = False
  elif failure == 'missing':
    modem.unlink()
  else:
    state[{'peer': 'peer', 'route': 'route', 'link': 'up'}[failure]] = '10.9.0.1' if failure == 'peer' else False
  if failure != 'missing':
    modem.write_text(json.dumps(value))
  diagnostics = policy.inspect(command)
  assert diagnostics['eligible'] is False
  assert diagnostics['failed_step'] is not None
  if failure in ('peer', 'route'):
    assert diagnostics['failed_step'] == 'ppp_policy_table'
  assert state['mutations'] == []


@pytest.mark.parametrize('failure', ['conflict', 'local_priority', 'onroad'])
def test_foreign_rules_and_offroad_gate_preserved(kernel, failure):
  state, command, gate, _ = kernel
  if failure == 'conflict':
    state['rules'].append({'priority': 18700, 'table': 42})
  elif failure == 'local_priority':
    state['rules'].pop(0)
  else:
    state['gate'] = False
  with pytest.raises(RuntimeError):
    policy.install(command, gate, 'usb0', [])
  assert state['mutations'] == []


def test_lost_reply_cleanup_only_removes_new_owned_policy(kernel):
  state, command, gate, _ = kernel
  foreign = {'priority': 18700, 'src': policy.JETSON, 'iif': 'other0', 'protocol': 187, 'table': 1000}
  assert not policy.ours(foreign, 'usb0', 18700)
  state['rules'].append({'priority': 15, 'table': 99})
  state['reply_lost'] = True
  added = []
  with pytest.raises(RuntimeError, match='reply lost'):
    policy.install(command, gate, 'usb0', added)
  state['reply_lost'] = False
  policy.remove(command, gate, 'usb0', added)
  assert [r['priority'] for r in state['rules']] == [0, 32766, 15]


@pytest.mark.parametrize('action,expected', [('unreachable', True), ('7', True), (7, True),
                                           ('blackhole', False), ('6', False), ('8', False)])
def test_numeric_iproute2_action_is_exactly_unreachable(action, expected):
  row = {'priority': 18701, 'src': policy.JETSON, 'iif': 'usb0', 'protocol': '187', 'action': action}
  assert policy.ours(row, 'usb0', 18701) is expected
  assert not policy.ours({**row, 'iif': 'wlan0'}, 'usb0', 18701)
