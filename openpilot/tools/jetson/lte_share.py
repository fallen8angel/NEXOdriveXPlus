"""Opt-in comma4 LTE -> verified Jetson USB-NCM internet gateway.

This tool is intentionally NOT automatic.  It never changes default routes on
comma4, USB gadgets, modeld, HUD, CAN, or cellular modem configuration.
Enable/disable require confirmed Offroad; status is read-only.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

COMMA = '192.168.100.127'
JETSON = '192.168.100.250'
TARGET = f'jetlink@{JETSON}'
PROTOCOL = 187
METRIC = 1900  # Worse than normal Jetson Wi-Fi; preserve existing default routes.
STATE = Path('/data/carrot/jetson-lte-share.json')
LOCK = Path('/data/carrot/jetson-lte-share.lock')
BOOT = Path('/proc/sys/kernel/random/boot_id')
IP_FORWARD = Path('/proc/sys/net/ipv4/ip_forward')
COMMENT = 'nexo-jetson-lte-v1'
FIREWALLS = ('iptables', 'iptables-legacy')
PROBE = Path('/data/carrot/jetson-lte-probe.json')


class GatewayError(RuntimeError):
  pass


def command(args, *, timeout=12, optional=False, stdin_text=None):
  try:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, input=stdin_text)
  except (OSError, subprocess.TimeoutExpired) as e:
    raise GatewayError(f'{args[0]} unavailable: {type(e).__name__}') from e
  if p.returncode and not optional:
    raise GatewayError(f'{args[0]} failed ({p.returncode}): {p.stderr.strip()[-240:]}')
  return p


def remote(*args, optional=False):
  ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
         '-o', 'ConnectTimeout=5', TARGET, shlex.join(args)]
  return command(ssh, timeout=18, optional=optional)


def json_output(result):
  try:
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, list):
      raise ValueError('expected array')
    return parsed
  except ValueError as e:
    raise GatewayError('Invalid network route response') from e


def offroad():
  try:
    return (Path('/data/params/d/IsOffroad').read_bytes().strip() == b'1'
            and Path('/data/params/d/IsOnroad').read_bytes().strip() == b'0')
  except OSError:
    return False


def require_offroad():
  if not offroad():
    raise GatewayError('Only run while fully Offroad (vehicle parked and off).')


def find_comma_ncm():
  # Reuse the already hardened g1/NCM discovery and direct USB route checks.
  from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
  iface = ncm.comma_interface()
  ncm.require_usb_route(iface, COMMA, JETSON)
  return iface


def cellular_wan():
  rows = json_output(command(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']))
  if len(rows) != 1:
    raise GatewayError('Cannot identify comma4 outgoing route')
  dev = rows[0].get('dev', '')
  # Never share Wi-Fi, USB, Ethernet or an unverified device by mistake.
  if dev == 'ppp0':
    # Comma4's PPPSession explicitly owns ppp0. Require a live PPP link,
    # rather than accepting arbitrary PPP/VPN interface names.
    links = json_output(command(['ip', '-j', '-d', 'link', 'show', 'dev', dev]))
    if (len(links) != 1 or links[0].get('link_type') != 'ppp'
        or not {'POINTOPOINT', 'UP'} <= set(links[0].get('flags', []))):
      raise GatewayError('Cellular default uplink ppp0 is not a live point-to-point PPP link')
  elif not re.fullmatch(r'(rmnet[\w.-]*|wwan[\w.-]*|ccmni[\w.-]*|pdp[\w.-]*)', dev):
    raise GatewayError(f'Cellular default uplink not confirmed (device={dev!r}). No changes made.')
  return dev


def sharing_uplink():
  """Use the real LTE default, or a VERIFIED existing PPP table when Wi-Fi wins."""
  try:
    return {'wan': cellular_wan(), 'route_mode': 'existing_default', 'eligible': True}
  except GatewayError as original:
    from openpilot.tools.jetson import lte_uplink
    observation = lte_uplink.inspect(command)
    if observation.get('eligible') is not True:
      raise GatewayError(str(original) + ' ' + observation.get('reason', '')) from original
    return observation


def jetson_ncm():
  rows = json_output(remote('ip', '-j', '-4', 'route', 'get', COMMA, 'from', JETSON))
  if len(rows) != 1 or rows[0].get('gateway') or rows[0].get('prefsrc', rows[0].get('src')) != JETSON:
    raise GatewayError('Jetson route to comma4 is not direct USB-NCM')
  iface = rows[0].get('dev', '')
  if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', iface):
    raise GatewayError('Invalid Jetson USB-NCM interface')
  driver = remote('readlink', '-f', f'/sys/class/net/{iface}/device/driver').stdout.strip()
  if Path(driver).name != 'cdc_ncm':
    raise GatewayError('Jetson interface is not the cdc_ncm USB link')
  addresses = json_output(remote('ip', '-j', '-4', 'addr', 'show', 'dev', iface))
  if not any(a.get('local') == JETSON and a.get('prefixlen') == 24
             for row in addresses for a in row.get('addr_info', [])):
    raise GatewayError('Jetson USB-NCM address is missing')
  if remote('cat', f'/sys/class/net/{iface}/carrier').stdout.strip() != '1':
    raise GatewayError('Jetson USB carrier not connected')
  remote('sudo', '-n', 'true')  # Fail before changing comma4 if Jetson cannot be configured.
  return iface


def jetson_routes():
  # iproute2 maps protocol 187 to the name "isis" by default. Request
  # numbers so an installed route is not mistaken for a missing route.
  return json_output(remote('ip', '-N', '-j', '-4', 'route', 'show', 'default'))


def managed_protocol(value):
  # Accept older captured responses too; identity still requires the exact
  # gateway, USB interface and dedicated metric in ours().
  return str(value) in (str(PROTOCOL), 'isis')


def ours(route, iface):
  return (route.get('dst') == 'default' and route.get('gateway') == COMMA
          and route.get('dev') == iface and route.get('metric') == METRIC
          and managed_protocol(route.get('protocol')))


def update_jetson_route(iface):
  routes = jetson_routes()
  if any(ours(route, iface) for route in routes):
    return False
  if any(managed_protocol(route.get('protocol')) for route in routes):
    raise GatewayError('A conflicting protocol-187 Jetson default route already exists')
  require_offroad()
  remote('sudo', '-n', 'ip', '-4', 'route', 'add', 'default', 'via', COMMA,
         'dev', iface, 'metric', str(METRIC), 'proto', str(PROTOCOL), 'onlink')
  return True


def remove_jetson_route(iface):
  try:
    if any(ours(route, iface) for route in jetson_routes()):
      require_offroad()
      remote('sudo', '-n', 'ip', '-4', 'route', 'del', 'default', 'via', COMMA,
             'dev', iface, 'metric', str(METRIC), 'proto', str(PROTOCOL))
    return True
  except GatewayError:
    return False


def rules(usb, wan):
  mark = ['-m', 'comment', '--comment', COMMENT]
  return [
    (['-t', 'nat', 'POSTROUTING'], ['-s', f'{JETSON}/32', '-o', wan, *mark, '-j', 'MASQUERADE']),
    (['FORWARD'], ['-i', usb, '-o', wan, '-s', f'{JETSON}/32', *mark, '-j', 'ACCEPT']),
    (['FORWARD'], ['-i', wan, '-o', usb, '-d', f'{JETSON}/32',
                   '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', *mark, '-j', 'ACCEPT']),
  ]


def select_firewall_backend(preferred=None):
  """Carrot's operational-backend probe, including the forwarding table.

  A saved backend is never silently switched: cleanup must target the table
  where our own rules were installed. No flush/policy/foreign-rule changes.
  """
  candidates = (preferred,) if preferred is not None else FIREWALLS
  for candidate in candidates:
    if candidate not in FIREWALLS:
      raise GatewayError('Invalid saved firewall backend')
    try:
      if all(command(['sudo', '-n', candidate, '-w', '2', *args], timeout=4, optional=True).returncode == 0
             for args in (['-t', 'nat', '-S', 'POSTROUTING'], ['-S', 'FORWARD'])):
        return candidate
    except GatewayError:
      continue
  raise GatewayError('No operational NAT/FORWARD backend (iptables or iptables-legacy); no changes made')


def iptables(chain, spec, action, *, optional=False):
  if action not in ('-C', '-S'):
    require_offroad()
  state = load() or {}
  backend = state.get('firewall_backend', 'iptables')
  if backend not in FIREWALLS:
    raise GatewayError('Invalid saved firewall backend')
  prefix, name = chain[:-1], chain[-1]
  return command(['sudo', '-n', backend, '-w', '2', *prefix, action, name, *spec],
                 timeout=4, optional=optional)


def present(chain, spec):
  result = iptables(chain, spec, '-C', optional=True)
  if result.returncode not in (0, 1):
    raise GatewayError('Firewall inspection failed: ' + str(result.stderr or '')[:240])
  return result.returncode == 0


def remove_rules(usb, wan):
  for chain, spec in reversed(rules(usb, wan)):
    if present(chain, spec):
      iptables(chain, spec, '-D')


def save(state):
  STATE.parent.mkdir(parents=True, exist_ok=True)
  tmp = STATE.with_suffix('.tmp')
  with tmp.open('w') as f:
    json.dump(state, f)
    f.flush()
    os.fsync(f.fileno())
  os.replace(tmp, STATE)
  directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
  try:
    os.fsync(directory)
  finally:
    os.close(directory)


def load():
  try:
    value = json.loads(STATE.read_text())
    if (not isinstance(value, dict)
        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value.get('usb', ''))
        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value.get('wan', ''))
        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value.get('host', ''))
        or value.get('firewall_backend', 'iptables') not in FIREWALLS):
      raise GatewayError('Unsafe existing gateway state; inspect manually')
    return value
  except FileNotFoundError:
    return None
  except (ValueError, TypeError) as e:
    raise GatewayError('Invalid existing gateway state') from e


def enable():
  require_offroad()
  usb = find_comma_ncm()
  uplink = sharing_uplink()
  wan, host = uplink['wan'], jetson_ncm()
  old = load()
  if old and ((old['usb'], old['wan'], old['host']) != (usb, wan, host)
              or old.get('route_mode', 'existing_default') != uplink['route_mode']):
    raise GatewayError('Saved interface changed; disable old gateway first')
  policy = uplink['route_mode'] == 'cellular_policy'
  if policy:
    from openpilot.tools.jetson import lte_uplink
    lte_uplink.preflight(command, usb)
  forward_before = IP_FORWARD.read_text().strip()
  if forward_before not in ('0', '1'):
    raise GatewayError('Cannot inspect IPv4 forwarding')
  backend = select_firewall_backend(old.get('firewall_backend', 'iptables') if old else None)
  state = dict(old) if old else {'usb': usb, 'wan': wan, 'host': host,
                  'previous_forward': forward_before, 'boot': BOOT.read_text().strip()}
  if state.get('boot') != BOOT.read_text().strip():
    state.update(previous_forward=forward_before, boot=BOOT.read_text().strip())
  state['firewall_backend'] = backend
  state['route_mode'] = uplink['route_mode']
  # Preserve pre-existing rules and routes if a retry fails partway through.
  existing_route = any(ours(row, host) for row in jetson_routes())
  added_rules = []
  added_policies = []
  changed_forward = False
  # Save intent before mutations so disable can clean up an interrupted attempt.
  PROBE.unlink(missing_ok=True)
  save(state)
  try:
    for chain, spec in rules(usb, wan):
      require_offroad()
      if not present(chain, spec):
        added_rules.append((chain, spec))
        iptables(chain, spec, '-I')  # Even a lost reply may have installed it.
    if policy:
      lte_uplink.install(command, require_offroad, usb, added_policies)
    require_offroad()
    if forward_before == '0':
      changed_forward = True
      command(['sudo', '-n', 'sysctl', '-w', 'net.ipv4.ip_forward=1'])
    update_jetson_route(host)
    require_offroad()
    verification = {'forwarding': IP_FORWARD.read_text().strip() == '1',
                    'rules': [present(chain, spec) for chain, spec in rules(usb, wan)],
                    'routes': jetson_routes(), 'uplink': sharing_uplink()['wan'],
                    'policy_verified': not policy or lte_uplink.verified(command, usb)}
    if (not verification['forwarding'] or not all(verification['rules'])
        or not any(ours(route, host) for route in verification['routes']) or verification['uplink'] != wan
        or not verification['policy_verified']):
      raise GatewayError('Gateway post-install verification failed: ' + json.dumps(verification))
  except Exception as error:
    rollback_errors = []
    # Once Onroad begins, defer ALL network cleanup too. Preserve the journal
    # for an explicit Offroad retry, rather than changing a live firewall.
    if not offroad():
      save({**state, 'recovery_pending': True, 'failure': str(error)[:240],
            'rollback_errors': ['Offroad lost; cleanup deferred']})
      raise
    # Route may have been installed even if the SSH reply was lost.
    if not existing_route and not remove_jetson_route(host):
      rollback_errors.append('Jetson route cleanup unconfirmed')
    if added_policies:
      try:
        lte_uplink.remove(command, require_offroad, usb, added_policies)
      except (GatewayError, RuntimeError) as cleanup_error:
        rollback_errors.append(str(cleanup_error)[:240])
    for chain, spec in reversed(added_rules):
      try:
        if present(chain, spec):
          iptables(chain, spec, '-D')
      except GatewayError as cleanup_error:
        rollback_errors.append(str(cleanup_error)[:240])
    if changed_forward:
      try:
        require_offroad()
        command(['sudo', '-n', 'sysctl', '-w', 'net.ipv4.ip_forward=0'])
      except GatewayError as cleanup_error:
        rollback_errors.append(str(cleanup_error)[:240])
    if rollback_errors:
      save({**state, 'recovery_pending': True, 'failure': str(error)[:240], 'rollback_errors': rollback_errors})
    elif old is None:
      STATE.unlink(missing_ok=True)
    else:
      save(old)
    raise
  save({**state, 'recovery_pending': False})
  return 'LTE USB gateway enabled; existing Wi-Fi route retained (metric 1900).'


def disable():
  require_offroad()
  state = load()
  if state is None:
    return 'No managed LTE gateway state.'
  PROBE.unlink(missing_ok=True)
  remote_ok = remove_jetson_route(state['host'])
  if state.get('route_mode') == 'cellular_policy':
    from openpilot.tools.jetson import lte_uplink
    lte_uplink.remove(command, require_offroad, state['usb'])
  remove_rules(state['usb'], state['wan'])
  if (state.get('previous_forward') == '0'
      and state.get('boot') == BOOT.read_text().strip()
      and IP_FORWARD.read_text().strip() == '1'):
    require_offroad()
    command(['sudo', '-n', 'sysctl', '-w', 'net.ipv4.ip_forward=0'])
  if remote_ok:
    STATE.unlink(missing_ok=True)
    return 'LTE USB gateway disabled; managed Jetson route removed.'
  return 'Local forwarding disabled, but Jetson route could not be verified. Retry while USB is connected.'


def status():
  # Read-only preflight: P gear alone is not fully Offroad. No networking
  # change is attempted by the status command.
  safe_to_change = offroad()
  gate = {'offroad': safe_to_change, 'can_change': safe_to_change,
          'change_requirement': '시동을 끄고 콤마4 IsOffroad=1, IsOnroad=0 상태에서만 공유 변경 가능'}
  errors = []
  try:
    state = load()
  except (GatewayError, OSError) as error:
    return {**gate, 'configured': None, 'enabled': False, 'state': 'unknown',
            'read_only': True, 'failures': [{'code': 'SAVED_STATE_INVALID', 'detail': str(error)[:240]}],
            'internet_dns_verified': False, 'operational': False}
  if not state:
    # Previously this early return hid the LTE interface and the real reason
    # enable would fail. These are local READS, never SSH or configuration.
    preflight = {}
    def inspect_uplink():
      uplink = sharing_uplink()
      preflight['uplink_diagnostics'] = uplink
      preflight['route_mode'] = uplink['route_mode']
      return uplink['wan']
    for key, inspect in (('active_cellular_wan', inspect_uplink), ('usb', find_comma_ncm),
                         ('firewall_backend', select_firewall_backend)):
      try:
        preflight[key] = inspect()
      except (GatewayError, OSError, RuntimeError, ValueError) as error:
        errors.append({'code': key.upper() + '_UNVERIFIED', 'detail': str(error)[:240]})
    return {**gate, 'configured': False, 'enabled': False, 'message': 'Opt-in gateway not configured',
            'update_note': 'Comma4 can download a pinned Jetson HUD release; Jetson Wi-Fi is not required for that download.',
            'lte_share_opt_in_only': True, 'state': 'not_configured', 'read_only': True, 'failures': errors,
            'internet_dns_verified': False, 'operational': False, **preflight}
  installed = []
  if state.get('recovery_pending'):
    errors.append({'code': 'ROLLBACK_PENDING', 'detail': 'Explicit Offroad cleanup/retry required; recovery journal preserved'})
  for chain, spec in rules(state['usb'], state['wan']):
    try:
      found = present(chain, spec)
      installed.append(found)
      if not found:
        errors.append({'code': 'FIREWALL_RULE_MISSING', 'detail': 'Managed forwarding rule absent'})
    except (GatewayError, OSError) as error:
      installed.append(None)
      errors.append({'code': 'FIREWALL_CHECK_FAILED', 'detail': str(error)[:240]})
  try:
    remote_route = any(ours(row, state['host']) for row in jetson_routes())
    remote_error = ''
    if not remote_route:
      errors.append({'code': 'JETSON_ROUTE_MISSING', 'detail': 'Managed USB default route not found'})
  except GatewayError as e:
    remote_route, remote_error = None, str(e)
    errors.append({'code': 'JETSON_SSH_OR_ROUTE_FAILED', 'detail': str(e)[:240]})
  try:
    forwarded = IP_FORWARD.read_text().strip() == '1'
    if not forwarded:
      errors.append({'code': 'IP_FORWARD_DISABLED', 'detail': 'Comma IPv4 forwarding is off'})
  except OSError as error:
    forwarded = None
    errors.append({'code': 'IP_FORWARD_UNAVAILABLE', 'detail': str(error)[:240]})
  # This observes the current comma uplink only; no route or NAT is changed.
  try:
    uplink = sharing_uplink()
    active_cellular_wan = uplink['wan']
  except GatewayError as error:
    uplink = {'eligible': False, 'reason': str(error)[:400]}
    active_cellular_wan = None
    errors.append({'code': 'CELLULAR_UPLINK_UNVERIFIED', 'detail': str(error)[:240]})
  if active_cellular_wan is not None and active_cellular_wan != state['wan']:
    errors.append({'code': 'CELLULAR_UPLINK_CHANGED', 'detail': 'Current cellular route differs from saved NAT interface'})
  mode_matches = uplink.get('route_mode') == state.get('route_mode', 'existing_default')
  if active_cellular_wan is not None and not mode_matches:
    errors.append({'code': 'CELLULAR_ROUTE_MODE_CHANGED',
                   'detail': 'LTE default/policy path changed; explicit Offroad disable and enable required'})
  policy_verified = True
  if state.get('route_mode') == 'cellular_policy':
    try:
      from openpilot.tools.jetson import lte_uplink
      policy_verified = lte_uplink.verified(command, state['usb'])
      if not policy_verified:
        errors.append({'code': 'LTE_POLICY_UNVERIFIED', 'detail': 'LTE source policy is missing or its existing PPP route disappeared'})
    except (RuntimeError, OSError, ValueError) as error:
      policy_verified = False
      errors.append({'code': 'LTE_POLICY_UNVERIFIED', 'detail': str(error)[:240]})
  probe_result = probe_record(state)
  # Installed firewall rules do not prove the current WAN and route policy work.
  enabled = bool(all(installed) and forwarded and remote_route is True and policy_verified)
  wan_route_matches = active_cellular_wan == state['wan'] and mode_matches
  operational = enabled and wan_route_matches and not errors
  return {**gate, 'read_only': True, 'state': 'degraded' if errors else 'configured_unverified_internet',
          'failures': errors, 'configured': True, 'enabled': enabled, 'operational': operational,
          'active_cellular_wan': active_cellular_wan,
          'uplink_diagnostics': uplink,
          'wan_route_matches_config': wan_route_matches,
          'internet_dns_verified': not errors and probe_result.get('internet_dns_verified') is True,
          'connectivity_probe': probe_result,
          'firewall_backend': state.get('firewall_backend', 'iptables'),
          'route_mode': state.get('route_mode', 'existing_default'), 'policy_verified': policy_verified,
          'recovery_pending': state.get('recovery_pending', False), 'rollback_errors': state.get('rollback_errors', []),
          'comma_forwarding': forwarded, 'firewall_rules': installed,
          'jetson_usb_route': remote_route, 'jetson_error': remote_error,
          'wan': state['wan'], 'usb': state['usb'], 'jetson_usb': state['host'],
          'note': 'Wi-Fi retains priority; DNS and live LTE data flow require device verification.'}


def probe_record(state):
  try:
    with PROBE.open() as stream:
      value = json.loads(stream.read(16385))
    age = time.monotonic() - value['checked_monotonic']
    if (value['boot'] != BOOT.read_text().strip() or not 0 <= age < 120
        or value['configuration'] != {key: state[key] for key in ('usb', 'wan', 'host')}):
      return {'state': 'expired', 'internet_dns_verified': False}
    return {**value, 'age_s': round(age, 1)}
  except (OSError, ValueError, TypeError, KeyError):
    return {'state': 'not_tested', 'internet_dns_verified': False}


def connectivity_probe():
  """Explicit Offroad test, not background traffic or network setup."""
  require_offroad()
  state = load()
  before = status()
  if not state or not before.get('enabled') or before.get('wan_route_matches_config') is not True:
    raise GatewayError('Enable and verify the LTE USB gateway before the bound Internet test')
  if jetson_ncm() != state['host']:
    raise GatewayError('Verified USB interface changed; no Internet probe sent')
  require_offroad()
  code = Path(__file__).with_name('lte_probe.py').read_text()
  result = command(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'ConnectTimeout=5', TARGET,
                    shlex.join(['sudo', '-n', 'python3', '-', state['host']])],
                   stdin_text=code, timeout=25)
  try:
    value = json.loads(result.stdout)
  except ValueError as error:
    raise GatewayError('Invalid USB Internet probe JSON') from error
  if (not isinstance(value, dict) or value.get('network_configuration_changed') is not False
      or value.get('source') != JETSON or value.get('interface') != state['host']):
    raise GatewayError('Invalid USB Internet probe response')
  after = status()
  valid = (offroad() and load() == state and after.get('enabled') is True
           and after.get('wan_route_matches_config') is True)
  value.update(state='measured' if valid else 'configuration_changed',
               internet_dns_verified=(valid and value.get('usb_dns_success') is True
                                      and value.get('usb_https_success') is True),
               checked_monotonic=time.monotonic(), boot=BOOT.read_text().strip(),
               configuration={key: state[key] for key in ('usb', 'wan', 'host')})
  PROBE.parent.mkdir(parents=True, exist_ok=True)
  temporary = PROBE.with_suffix('.tmp')
  temporary.write_text(json.dumps(value, ensure_ascii=False))
  temporary.replace(PROBE)
  return value


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=['status', 'enable', 'disable', 'probe'])
  args = parser.parse_args()
  try:
    if args.action == 'status':
      print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
      STATE.parent.mkdir(parents=True, exist_ok=True)
      with LOCK.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'probe':
          print(json.dumps(connectivity_probe(), ensure_ascii=False))
        else:
          print(enable() if args.action == 'enable' else disable())
  except (GatewayError, OSError) as e:
    print(f'JETSON_LTE_GATEWAY_ERROR: {e}', file=sys.stderr)
    return 1
  return 0


if __name__ == '__main__':
  sys.exit(main())
