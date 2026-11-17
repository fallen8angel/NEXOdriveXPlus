"""Verified existing Comma4 PPP table; narrowly owned USB source rules only.

Never creates a default route or changes Wi-Fi/modem configuration. A paired
unreachable rule prevents fall-through to Wi-Fi if the LTE route disappears.
"""
import ipaddress
import json
from pathlib import Path
import time

JETSON = '192.168.100.250'
TABLE = 1000  # PPPSession.maybe_install_routes owns this existing table.
PRIORITIES = (18700, 18701)
PROTOCOL = 187
MODEM = Path('/dev/shm/modem')


def rows(command, args):
  value = json.loads(command(['ip', '-N', '-j', '-4', *args], timeout=3).stdout)
  if not isinstance(value, list):
    raise ValueError('Invalid kernel network response')
  return value


def ppp_peer_addresses(addresses, local):
  """Read the remote PPP endpoint from supported iproute2 JSON schemas.

  iproute2 prints the PPP peer under 'address' in JSON even though the
  human-readable output says 'peer'. Older mocked/provided schemas can use
  'peer'. Never infer the remote peer from the default route itself.
  """
  peers = set()
  for row in addresses:
    if row.get('ifname', 'ppp0') != 'ppp0':
      continue
    for item in row.get('addr_info', []):
      if item.get('family', 'inet') != 'inet' or item.get('local') != local:
        continue
      raw_peers = [item[key] for key in ('address', 'peer') if item.get(key)]
      if not raw_peers:
        continue
      try:
        parsed = {str(ipaddress.IPv4Address(str(raw).split('/')[0])) for raw in raw_peers}
      except (ValueError, ipaddress.AddressValueError):
        continue
      # Conflicting aliases or a local loopback cannot authorize forwarding.
      if len(parsed) == 1 and local not in parsed:
        peers.update(parsed)
  return peers


def inspect(command):
  result = {'eligible': False, 'wan': 'ppp0', 'route_mode': 'cellular_policy', 'table': TABLE,
            'network_configuration_changed': False, 'failed_step': 'default_route'}
  try:
    current = rows(command, ['route', 'get', '1.1.1.1'])
    result['default_interface'] = current[0].get('dev') if len(current) == 1 else None
    result['default_routes'] = [{k: r[k] for k in ('dev', 'gateway', 'metric') if k in r}
                                 for r in rows(command, ['route', 'show', 'default'])[:8]]
    if result['default_interface'] not in ('wlan0', 'ppp0'):
      raise ValueError('기존 기본 경로가 검증 대상 Wi-Fi/LTE 경로가 아닙니다.')
    result['failed_step'] = 'modem_freshness'
    with MODEM.open() as source:
      modem = json.loads(source.read(16385))
    stamp = modem.get('seconds_since_boot')
    age = time.monotonic() - stamp if type(stamp) in (int, float) else None
    result['modem'] = {k: modem.get(k) for k in ('connected', 'state', 'network_type')}
    result['modem']['age_s'] = age
    if modem.get('connected') is not True or age is None or not 0 <= age < 15:
      raise ValueError('최신 LTE 모뎀 연결 증거가 없습니다. SIM·LTE 등록·PPP 연결 상태를 확인하세요.')
    result['failed_step'] = 'ppp_link'
    address = str(ipaddress.IPv4Address(modem.get('ip_address', '')))
    links = json.loads(command(['ip', '-j', '-d', 'link', 'show', 'dev', 'ppp0'], timeout=3).stdout)
    if len(links) != 1 or links[0].get('link_type') != 'ppp' or not {'UP', 'POINTOPOINT'} <= set(links[0].get('flags', [])):
      raise ValueError('LTE PPP 링크가 활성 상태가 아닙니다.')
    result['failed_step'] = 'ppp_address'
    addresses = rows(command, ['addr', 'show', 'dev', 'ppp0'])
    peers = ppp_peer_addresses(addresses, address)
    result['failed_step'] = 'ppp_policy_table'
    defaults = [r for r in rows(command, ['route', 'show', 'table', str(TABLE)]) if r.get('dst') == 'default']
    result['policy_route'] = {'default_count': len(defaults),
                              'interface': defaults[0].get('dev') if len(defaults) == 1 else None,
                              'gateway_matches_peer': defaults[0].get('gateway') in peers if len(defaults) == 1 else False,
                              'ppp_address_matches_modem': bool(peers)}
    if len(defaults) != 1 or defaults[0].get('dev') != 'ppp0' or defaults[0].get('gateway') not in peers:
      raise ValueError('모뎀이 관리하는 기존 LTE table 1000·PPP 주소·peer가 일치하지 않습니다.')
    result.update(eligible=True, failed_step=None,
                  reason='LTE PPP와 기존 table 1000 확인 · Wi-Fi 기본 경로 유지 가능')
  except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
    result['reason'] = 'LTE 정책 경로 미검증: ' + str(error)[:240]
  return result


def rule_rows(command):
  return rows(command, ['rule', 'show'])


def ours(row, usb, priority):
  source = row.get('src', row.get('from'))
  base = (row.get('priority') == priority and source in (JETSON, JETSON + '/32')
          and row.get('iif', row.get('iifname')) == usb
          and str(row.get('protocol')) in ('187', 'isis')
          and row.get('dst', 'all') in ('all', '0.0.0.0/0')
          and not any(row.get(k) for k in ('fwmark', 'fwmask', 'ipproto', 'sport', 'dport', 'uidrange', 'oif', 'tos')))
  if not base:
    return False
  if priority == PRIORITIES[0]:
    return str(row.get('table')) == str(TABLE)
  # ip -N prints the kernel FR_ACT_UNREACHABLE enum (7), including JSON.
  return str(row.get('action', row.get('type'))) in ('unreachable', '7')


def preflight(command, usb):
  existing = rule_rows(command)
  if not any(r.get('priority') == 0 and str(r.get('table')) in ('255', 'local') for r in existing):
    raise RuntimeError('LTE_LOCAL_RULE_UNVERIFIED: USB 로컬 SSH/HUD 경로 우선순위가 확인되지 않았습니다.')
  for row in existing:
    if row.get('priority') in PRIORITIES and not ours(row, usb, row['priority']):
      # Existing matching source/interface/table alone is not ownership.
      # Only protocol 187 marks rules created by our managed installer.
      # Never delete, adopt, or replace unidentified preexisting rules.
      priority = row['priority']
      protocol = row.get('protocol')
      reason = '프로토콜 식별자 없음' if protocol is None else f'프로토콜 불일치({protocol})'
      raise RuntimeError(
        f'LTE_POLICY_RULE_CONFLICT: 우선순위 {priority} 기존 규칙의 소유권 미확인 ({reason}). '
        '기존 라우팅 정책은 보존합니다. 자동 교체·삭제하지 않습니다.')
  return existing


def rule_command(command, gate, action, usb, priority):
  gate()
  args = ['sudo', '-n', 'ip', '-4', 'rule', action, 'pref', str(priority), 'from', JETSON + '/32',
          'iif', usb, 'protocol', str(PROTOCOL)]
  args += ['lookup', str(TABLE)] if priority == PRIORITIES[0] else ['unreachable']
  command(args, timeout=4)


def install(command, gate, usb, added):
  existing = preflight(command, usb)
  for priority in reversed(PRIORITIES):  # Reject fallback before adding LTE lookup.
    if not any(ours(r, usb, priority) for r in existing):
      added.append(priority)  # Record intent even if a command reply is lost.
      rule_command(command, gate, 'add', usb, priority)
  if not all(any(ours(r, usb, p) for r in rule_rows(command)) for p in PRIORITIES):
    raise RuntimeError('LTE_POLICY_VERIFY_FAILED: ' + json.dumps(rule_rows(command)))


def remove(command, gate, usb, priorities=PRIORITIES):
  for priority in sorted(priorities):  # Remove lookup before removing its fallback guard.
    if any(ours(r, usb, priority) for r in rule_rows(command)):
      rule_command(command, gate, 'del', usb, priority)


def verified(command, usb):
  existing = rule_rows(command)
  if not all(any(ours(r, usb, priority) for r in existing) for priority in PRIORITIES):
    return False
  route = rows(command, ['route', 'get', '1.1.1.1', 'from', JETSON, 'iif', usb])
  return len(route) == 1 and route[0].get('dev') == 'ppp0' and str(route[0].get('table')) == str(TABLE)
