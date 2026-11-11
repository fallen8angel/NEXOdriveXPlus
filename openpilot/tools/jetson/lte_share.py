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


class GatewayError(RuntimeError):
  pass


def command(args, *, timeout=12, optional=False):
  try:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
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
  if not re.fullmatch(r'(rmnet[\w.-]*|wwan[\w.-]*|ccmni[\w.-]*|pdp[\w.-]*)', dev):
    raise GatewayError(f'Cellular default uplink not confirmed (device={dev!r}). No changes made.')
  return dev


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
  return json_output(remote('ip', '-j', '-4', 'route', 'show', 'default'))


def ours(route, iface):
  return (route.get('dst') == 'default' and route.get('gateway') == COMMA
          and route.get('dev') == iface and route.get('metric') == METRIC
          and str(route.get('protocol')) == str(PROTOCOL))


def update_jetson_route(iface):
  routes = jetson_routes()
  if any(ours(route, iface) for route in routes):
    return False
  if any(str(route.get('protocol')) == str(PROTOCOL) for route in routes):
    raise GatewayError('A conflicting protocol-187 Jetson default route already exists')
  require_offroad()
  remote('sudo', '-n', 'ip', '-4', 'route', 'add', 'default', 'via', COMMA,
         'dev', iface, 'metric', str(METRIC), 'proto', str(PROTOCOL), 'onlink')
  return True


def remove_jetson_route(iface):
  try:
    if any(ours(route, iface) for route in jetson_routes()):
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


def iptables(chain, spec, action, *, optional=False):
  prefix, name = chain[:-1], chain[-1]
  return command(['sudo', '-n', 'iptables', '-w', *prefix, action, name, *spec],
                 optional=optional)


def present(chain, spec):
  return iptables(chain, spec, '-C', optional=True).returncode == 0


def remove_rules(usb, wan):
  for chain, spec in reversed(rules(usb, wan)):
    if present(chain, spec):
      iptables(chain, spec, '-D')


def save(state):
  STATE.parent.mkdir(parents=True, exist_ok=True)
  tmp = STATE.with_suffix('.tmp')
  with tmp.open('w') as f:
    json.dump(state, f)
  os.replace(tmp, STATE)


def load():
  try:
    value = json.loads(STATE.read_text())
    if (not isinstance(value, dict)
        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value.get('usb', ''))
        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value.get('wan', ''))
        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value.get('host', ''))):
      raise GatewayError('Unsafe existing gateway state; inspect manually')
    return value
  except FileNotFoundError:
    return None
  except (ValueError, TypeError) as e:
    raise GatewayError('Invalid existing gateway state') from e


def enable():
  require_offroad()
  usb, wan, host = find_comma_ncm(), cellular_wan(), jetson_ncm()
  old = load()
  if old and (old['usb'], old['wan'], old['host']) != (usb, wan, host):
    raise GatewayError('Saved interface changed; disable old gateway first')
  forward_before = IP_FORWARD.read_text().strip()
  if forward_before not in ('0', '1'):
    raise GatewayError('Cannot inspect IPv4 forwarding')
  state = old or {'usb': usb, 'wan': wan, 'host': host,
                  'previous_forward': forward_before, 'boot': BOOT.read_text().strip()}
  # Preserve pre-existing rules and routes if a retry fails partway through.
  existing_route = any(ours(row, host) for row in jetson_routes())
  added_rules = []
  changed_forward = False
  # Save intent before mutations so disable can clean up an interrupted attempt.
  save(state)
  try:
    for chain, spec in rules(usb, wan):
      require_offroad()
      if not present(chain, spec):
        iptables(chain, spec, '-I')
        added_rules.append((chain, spec))
    require_offroad()
    if forward_before == '0':
      command(['sudo', '-n', 'sysctl', '-w', 'net.ipv4.ip_forward=1'])
      changed_forward = True
    update_jetson_route(host)
  except Exception:
    # Route may have been installed even if the SSH reply was lost.
    if not existing_route:
      remove_jetson_route(host)
    for chain, spec in reversed(added_rules):
      try:
        if present(chain, spec):
          iptables(chain, spec, '-D')
      except GatewayError:
        pass
    if changed_forward:
      command(['sudo', '-n', 'sysctl', '-w', 'net.ipv4.ip_forward=0'], optional=True)
    if old is None:
      STATE.unlink(missing_ok=True)
    raise
  return 'LTE USB gateway enabled; existing Wi-Fi route retained (metric 1900).'


def disable():
  require_offroad()
  state = load()
  if state is None:
    return 'No managed LTE gateway state.'
  remote_ok = remove_jetson_route(state['host'])
  remove_rules(state['usb'], state['wan'])
  if (state.get('previous_forward') == '0'
      and state.get('boot') == BOOT.read_text().strip()
      and IP_FORWARD.read_text().strip() == '1'):
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
  state = load()
  if not state:
    return {**gate, 'configured': False, 'enabled': False, 'message': 'Opt-in gateway not configured',
            'update_note': 'Comma4 can download a pinned Jetson HUD release; Jetson Wi-Fi is not required for that download.',
            'lte_share_opt_in_only': True}
  installed = [present(chain, spec) for chain, spec in rules(state['usb'], state['wan'])]
  try:
    remote_route = any(ours(row, state['host']) for row in jetson_routes())
    remote_error = ''
  except GatewayError as e:
    remote_route, remote_error = None, str(e)
  forwarded = IP_FORWARD.read_text().strip() == '1'
  return {**gate, 'configured': True, 'enabled': all(installed) and forwarded and remote_route is True,
          'comma_forwarding': forwarded, 'firewall_rules': installed,
          'jetson_usb_route': remote_route, 'jetson_error': remote_error,
          'wan': state['wan'], 'usb': state['usb'], 'jetson_usb': state['host'],
          'note': 'Wi-Fi retains priority; DNS and live LTE data flow require device verification.'}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=['status', 'enable', 'disable'])
  args = parser.parse_args()
  try:
    if args.action == 'status':
      print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
      STATE.parent.mkdir(parents=True, exist_ok=True)
      with LOCK.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(enable() if args.action == 'enable' else disable())
  except (GatewayError, OSError) as e:
    print(f'JETSON_LTE_GATEWAY_ERROR: {e}', file=sys.stderr)
    return 1
  return 0


if __name__ == '__main__':
  sys.exit(main())
