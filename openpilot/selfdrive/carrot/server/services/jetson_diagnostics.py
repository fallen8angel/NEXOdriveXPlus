"""Read-only USB-NCM and Jetlink troubleshooting for the port-7000 web UI.

Never opens a Jetlink TCP session, changes a USB gadget, or configures routes.
Peer state is unknown unless fresh independent telemetry proves it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import subprocess

from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
from openpilot.tools.jetson.state import public_text


def _read(path):
  try:
    return path.read_text(encoding='utf-8').strip()[:160]
  except (OSError, UnicodeError):
    return None


def _ip_json(*args):
  try:
    process = subprocess.run(['ip', '-j', '-4', *args], capture_output=True, text=True,
                             timeout=0.75, check=False)
    if process.returncode:
      return None
    rows = json.loads(process.stdout)
    return rows if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else None
  except (OSError, ValueError, subprocess.SubprocessError):
    return None


def _git_value(*args):
  try:
    root = Path(__file__).resolve().parents[5]
    process = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                             text=True, timeout=0.75, check=False)
    return process.stdout.strip()[:100] if process.returncode == 0 else ''
  except (OSError, subprocess.SubprocessError):
    return ''


def _ncm_observation():
  """Read only bound gadget NCM, controller, sysfs and interface route information."""
  bound = []
  controllers = []
  try:
    for gadget in sorted(ncm.GADGET_ROOT.glob('*/UDC'))[:8]:
      name = _read(gadget)
      if name is None:
        continue
      if name:
        bound.append(gadget.parent.name)
        udc = Path('/sys/class/udc') / name
        controllers.append({'controller': name[:80], 'state': _read(udc / 'state'),
                            'speed': _read(udc / 'current_speed')})
    functions = []
    for item in sorted(ncm.GADGET_ROOT.glob('*/configs/*/ncm.*'))[:16]:
      if item.parents[2].name not in bound:
        continue
      iface = _read(item / 'ifname')
      if iface and iface.isascii() and iface.replace('_', '').replace('-', '').isalnum() and (ncm.NET_CLASS / iface).is_dir():
        functions.append(iface)
    interfaces = sorted(set(functions))
  except OSError:
    interfaces = []
  # Do not mistake a generic usb0 for an active NCM function.
  iface = interfaces[0] if len(interfaces) == 1 else None
  address = None
  route = None
  if iface:
    rows = _ip_json('addr', 'show', 'dev', iface)
    if rows is not None:
      ips = [addr.get('local') for row in rows if row.get('ifname') == iface
             for addr in row.get('addr_info', []) if isinstance(addr, dict) and addr.get('family') == 'inet']
      address = next((ip for ip in ips if ip == ncm.COMMA_ADDR), ips[0] if ips else '')
    if address == ncm.COMMA_ADDR:
      paths = _ip_json('route', 'get', ncm.JETSON_ADDR, 'from', address)
      if paths is not None:
        route = any(row.get('dev') == iface and not row.get('gateway')
                    and row.get('prefsrc', row.get('src', address)) == address for row in paths)
  # The real bound interface is inspected even when Jetlink inference is OFF.
  # This avoids assuming that an unrelated interface named usb0 is the Jetson.
  ncm_admin_up = None
  ncm_carrier = None
  if iface:
    flags = _read(ncm.NET_CLASS / iface / 'flags')
    try:
      parsed_flags = int(flags, 16)
      ncm_admin_up = bool(parsed_flags & 0x1)
    except (TypeError, ValueError):
      parsed_flags = None
    carrier = _read(ncm.NET_CLASS / iface / 'carrier')
    ncm_carrier = {'1': True, '0': False}.get(carrier)
    if ncm_admin_up is False:
      ncm_carrier = False
    elif ncm_carrier is None and parsed_flags is not None:
      ncm_carrier = bool(parsed_flags & 0x10000)
  neighbor = None
  if iface:
    rows = _ip_json('neigh', 'show', 'to', ncm.JETSON_ADDR, 'dev', iface)
    if rows is not None:
      neighbor = next((str(row.get('state') or 'UNKNOWN')[:24] for row in rows
                       if row.get('dst') == ncm.JETSON_ADDR), 'NONE')
  return {'udc_bound': bool(bound), 'gadget_owners': bound[:8],
          'ncm_admin_up': ncm_admin_up, 'ncm_carrier': ncm_carrier,
          'neighbor_cache_state': neighbor, 'controllers': controllers, 'ncm_interfaces': interfaces,
          'ncm_interface': iface, 'comma_ipv4': address, 'expected_comma_ipv4': ncm.COMMA_ADDR,
          'expected_jetson_ipv4': ncm.JETSON_ADDR, 'peer_route_via_usb': route}


def build_report(result, local, receiver_active=False, include_git=True):
  """Only observable facts; no control actions or speculative failure claims."""
  usb = _ncm_observation()
  native = result.get('native_link') or {}
  status = result.get('status') or {}
  recent = result.get('recent') or {}
  health = result.get('health') or {}
  stages = {s.get('id'): s for s in result.get('stages', []) if isinstance(s, dict)}
  try:
    mode = ncm.transport_mode()
  except (OSError, ValueError, RuntimeError):
    mode = 'unknown'
  issues = []
  if local.get('conflict'):
    issues.append('Jetlink 오프로딩과 NexoJetsonUsb 표시 모드가 동시에 활성화되어 있습니다.')
  if mode == 'ncm-tcp':
    if not usb['udc_bound']:
      issues.append('USB Gadget UDC 바인딩이 확인되지 않았습니다.')
    if len(usb['ncm_interfaces']) != 1:
      issues.append('바인딩된 USB NCM 인터페이스를 정확히 한 개 확인하지 못했습니다.')
    if usb['ncm_interface'] and usb['comma_ipv4'] != ncm.COMMA_ADDR:
      issues.append('콤마4 USB NCM IPv4 주소가 예상한 주소와 다르거나 확인되지 않았습니다.')
    if usb['comma_ipv4'] == ncm.COMMA_ADDR and usb['peer_route_via_usb'] is not True:
      issues.append('젯슨 전용 USB 경로가 확인되지 않았습니다.')
  if local.get('usb_admin_up') is False:
    issues.append('USB 인터페이스가 관리상 DOWN 상태입니다.')
  elif local.get('usb_carrier') is False:
    issues.append('USB 링크 carrier가 연결되지 않았습니다. 전원 상태는 이 정보만으로 판단할 수 없습니다.')
  if result.get('connected') is not True:
    issues.append('최근 젯슨 앱/Jetlink 응답이 확인되지 않았습니다. 전원 및 케이블 고장은 확정할 수 없습니다.')
  if not receiver_active:
    issues.append('7000 서버 젯슨 상태 비콘 수신기가 활성화되지 않았습니다.')
  inference = ('비활성' if local.get('enabled') is False else
               '실행 확인' if status.get('model_active') is True else
               '대기 또는 확인 불가')
  return {
    'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'read_only': True,
    'git': ({'branch': _git_value('rev-parse', '--abbrev-ref', 'HEAD'),
             'commit': _git_value('rev-parse', 'HEAD')} if include_git else {'branch': '', 'commit': ''}),
    'mode': {'transport': mode, 'jetlink_inference_enabled': local.get('enabled'),
             'legacy_display_enabled': local.get('display_usb'), 'onroad': local.get('onroad')},
    'usb_gadget': usb,
    'usb_link': {key: local.get(key) for key in ('usb_present', 'usb_interface', 'usb_admin_up',
                'usb_admin_state', 'usb_operstate', 'usb_carrier', 'usb_carrier_source', 'usb_discovery_error')},
    'jetlink': {'native_state': native.get('state'), 'native_transport': native.get('transport'),
                'native_stage': native.get('stage'), 'native_wait_reason': native.get('wait_reason'),
                'peer_telemetry_fresh': result.get('connected') is True, 'receiver_active': bool(receiver_active),
                'last_beacon_age_ms': result.get('age_ms')},
    'pipeline': {'stages': list(stages.values()), 'camera_tx_recent': result.get('camera_tx_recent'),
                 'hud_tx_recent': result.get('hud_tx_recent'), 'recent': recent,
                 'model_ready': status.get('model_ready'), 'model_active': status.get('model_active'),
                 'inference': inference, 'jetson_ip_reported': status.get('ip'),
                 'jetson_temperature_c': health.get('temperature_c'),
                 'physical_hud_display_verified': False},
    'reported_error': str(status.get('last_error') or '')[:300],
    'observations': issues[:8] or ['현재 조회 가능한 항목에서 직접적인 오류 근거를 찾지 못했습니다. 실제 젯슨 동작은 별도 검증이 필요합니다.'],
    'notice': '읽기 전용 진단입니다. 원격 젯슨 전원, 실제 HUD 화면, TCP/HELLO는 유효한 최근 응답 없이는 검증할 수 없습니다.',
  }


def report_text(report):
  lines = ['NEXO Jetson · 7000 읽기 전용 진단', '검사 시각(UTC): '+report['generated_at_utc'],
           '브랜치: '+(report['git']['branch'] or '확인 불가'),
           '커밋: '+(report['git']['commit'] or '확인 불가')]
  for section in ('mode', 'usb_gadget', 'usb_link', 'jetlink', 'pipeline'):
    lines.extend(('', '['+section+']', json.dumps(report[section], ensure_ascii=False, indent=2)))
  lines.extend(('', '[관측 결과]', *(f'- {value}' for value in report['observations']),
                '', '보고된 오류: '+(report['reported_error'] or '없음'), report['notice']))
  return '\n'.join(lines)+'\n'

# Augment the existing port-7000 diagnostic without changing its legacy API keys.
# All observations are passive; no second Jetlink TCP/HELLO connection is opened.
NETWORK_STATUS = Path('/dev/shm/nexo-jetlink-network-status.json')
_legacy_build_report = build_report
_legacy_report_text = report_text


def network_recovery():
  try:
    if NETWORK_STATUS.is_symlink() or NETWORK_STATUS.stat().st_size > 8192:
      return {'state': 'unknown', 'fresh': False}
    with NETWORK_STATUS.open('rb') as stream:
      data = json.loads(stream.read(8193))
    updated = data.get('updated_mono')
    fresh = type(updated) in (float, int) and math.isfinite(updated) and 0 <= time.monotonic() - updated < 18
    return {'state': str(data.get('state') or 'unknown')[:32] if fresh else 'unknown',
            'fresh': bool(fresh),
            'attempts': data.get('attempts') if type(data.get('attempts')) is int else None,
            'recoveries': data.get('recoveries') if type(data.get('recoveries')) is int else None,
            'last_error': public_text(data.get('last_error'), 180) if fresh else ''}
  except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
    return {'state': 'unknown', 'fresh': False}


def classify(usb, local, native, result, mode):
  """Priority-ordered fault diagnosis. Peer beacon is never HELLO proof."""
  iface = usb.get('ncm_interface')
  if local.get('conflict'):
    return 'DISPLAY_CONFLICT', '표시 전용 USB와 Jetlink 추론 설정이 충돌합니다.', '정차 상태에서 USB 소유자와 모드 설정을 확인하십시오.'
  link_admin = usb.get('ncm_admin_up')
  link_carrier = usb.get('ncm_carrier')
  if link_admin is None:
    link_admin = local.get('usb_admin_up')
  if link_carrier is None:
    link_carrier = local.get('usb_carrier')
  if mode != 'ncm-tcp':
    if iface and link_admin is False:
      return 'USB_ADMIN_DOWN', 'NCM 인터페이스가 DOWN 상태입니다.', '기존 NCM 자동 복구 이력을 확인하십시오.'
    if iface and link_carrier is False:
      return 'USB_LINK_DOWN', 'NCM 케이블 링크의 carrier가 연결되지 않았습니다.', '정차 후 USB 케이블과 젯슨 USB 호스트 포트를 확인하십시오.'
    if local.get('enabled') is False:
      return 'INFERENCE_OFF', '외부 추론은 OFF입니다. USB 감지는 별도 진행됩니다.', '연결 진단은 가능하며 추론은 자동 활성화하지 않습니다.'
    return 'OTHER_TRANSPORT', '전용 USB bulk 전송 모드입니다.', 'AGNOS g1의 UDC 점유와 FunctionFS 충돌 여부를 확인하십시오.'
  if not usb.get('udc_bound'):
    return 'UDC_NOT_BOUND', 'USB Gadget 컨트롤러 바인딩이 확인되지 않습니다.', '기존 g1 소유 상태를 확인하십시오. 진단은 USB 설정을 바꾸지 않습니다.'
  if len(usb.get('ncm_interfaces') or []) > 1:
    return 'NCM_AMBIGUOUS', '활성 NCM 인터페이스가 여러 개입니다.', '실제 g1에 연결된 NCM 인터페이스를 확인하십시오.'
  if not iface:
    return 'NCM_MISSING', '바인딩된 NCM 인터페이스를 찾지 못했습니다.', 'AGNOS g1/ncm.0 구성을 확인하십시오.'
  if link_admin is False:
    return 'USB_ADMIN_DOWN', 'NCM 인터페이스가 DOWN 상태입니다.', '기존 NCM 자동 복구 진행 상태를 확인하십시오.'
  if link_carrier is False:
    return 'USB_LINK_DOWN', 'NCM USB 물리 링크가 연결되지 않았습니다.', '정차 후 USB 데이터 케이블과 젯슨 USB 호스트 포트를 확인하십시오.'
  if usb.get('comma_ipv4') != ncm.COMMA_ADDR:
    return 'NCM_IP_MISSING', '콤마4 전용 NCM IP 주소가 확인되지 않습니다.', 'USB IP 복구 이력을 확인하십시오. Wi-Fi로 우회하지 마십시오.'
  if usb.get('peer_route_via_usb') is not True:
    return 'NCM_ROUTE_MISSING', '젯슨으로 가는 USB 전용 경로가 확인되지 않습니다.', 'g1 NCM 네트워크 복구 이력과 전용 /32 경로를 확인하십시오.'
  if native.get('severity') == 'error':
    return 'NATIVE_ERROR', 'Jetlink가 오류를 보고했습니다.', 'Jetlink 단계별 오류와 모델 SHA·TensorRT 엔진을 확인하십시오.'
  if native.get('state') == 'ready' and native.get('transport') == 'ncm-tcp':
    return 'NATIVE_READY', 'NCM-TCP Jetlink 연결 준비가 확인됐습니다.', '실제 추론과 HUD 출력은 별도로 정차 검증하십시오.'
  if result.get('connected') is True:
    return 'PEER_BEACON_ONLY', '젯슨 상태 신호만 확인됐습니다. NCM HELLO는 미검증입니다.', '젯슨 서버의 USB 192.168.100.250:5599 TCP 실행 여부를 확인하십시오.'
  if local.get('enabled') is False:
    return 'NCM_LINK_READY', 'USB NCM 회선은 준비됐지만 외부 추론은 OFF입니다.', '기본 모델을 유지하십시오. 실제 통신 시험 후 정차 상태에서만 추론을 별도 선택하십시오.'
  return 'WAITING_FOR_JETSON', 'USB 경로는 준비됐지만 젯슨 서버 응답이 없습니다.', '젯슨 NCM IP·5599/TCP 서버·모델 SHA를 확인하십시오.'



def connection_evidence(result, local, usb):
  """No inferential shortcuts: a carrier, NCM route, HELLO and inference differ."""
  native = result.get('native_link') or {}
  carrier = local.get('usb_carrier') if type(local.get('usb_carrier')) is bool else None
  ncm_carrier = usb.get('ncm_carrier') if type(usb.get('ncm_carrier')) is bool else None
  iface = usb.get('ncm_interface')
  ncm_path = (usb.get('udc_bound') is True and iface is not None and
              len(usb.get('ncm_interfaces') or []) == 1 and
              usb.get('ncm_admin_up') is True and ncm_carrier is True and
              usb.get('comma_ipv4') == ncm.COMMA_ADDR and
              usb.get('peer_route_via_usb') is True)
  hello = bool(result.get('connected') is True and native.get('state') == 'ready' and
               native.get('transport') == 'ncm-tcp')
  peer = result.get('connected') is True
  carrier_only = carrier is True and not ncm_path
  level = ('NCM_HELLO_CONFIRMED' if hello else 'PEER_TELEMETRY_ONLY' if peer else
           'NCM_ROUTE_AND_CARRIER' if ncm_path else
           'INTERFACE_CARRIER_ONLY' if carrier is True or ncm_carrier is True else
           'NO_PHYSICAL_PROOF')
  return {
    'level': level,
    'interface_carrier': carrier,
    'interface_name': local.get('usb_interface') or None,
    'interface_source': local.get('usb_carrier_source') or 'unknown',
    'g1_ncm_udc_bound': usb.get('udc_bound') is True,
    'g1_ncm_interface': iface,
    'g1_ncm_carrier': ncm_carrier,
    'ncm_route_and_carrier_ready': ncm_path,
    'jetson_app_recent': peer,
    'ncm_hello_verified': hello,
    'camera_tx_recent': result.get('camera_tx_recent') is True if peer else False,
    'hud_tx_recent': result.get('hud_tx_recent') is True if peer else False,
    'inference_enabled': local.get('enabled') is True,
    'inference_active_reported': result.get('status', {}).get('model_active') is True if peer else False,
    'note': ('상태 신호와 네트워크 관측은 추론/실제 HUD 출력 증명이 아닙니다. '
             '단독 USB carrier만으로 젯슨의 물리적 식별도 확정하지 않습니다.'),
    'carrier_not_ncm_proof': carrier_only,
  }


def build_report(result, local, receiver_active=False, include_git=True):
  value = _legacy_build_report(result, local, receiver_active, include_git=include_git)
  usb = value['usb_gadget']
  native = result.get('native_link') or {}
  status = result.get('status') or {}
  health = result.get('health') or {}
  mode = value['mode']['transport']
  code, reason, advice = classify(usb, local, native, result, mode)
  peer_fresh = result.get('connected') is True
  recovery = network_recovery()
  value['assessment'] = {'code': code, 'reason': reason, 'recommended_action': advice}
  value['network_recovery'] = recovery
  value['connection_evidence'] = connection_evidence(result, local, usb)
  value['remote_boot_and_services'] = {
    'recent_heartbeat': peer_fresh,
    'boot_completed': None,  # No OS-ready proof available without a fresh explicit status.
    'reported_app_active': status.get('service_active') if peer_fresh and type(status.get('service_active')) is bool else None,
    'carrot_jetlink_service': None,  # Individual systemd units are not visible when the Jetson is offline.
    'carrot_jetlink_hud_service': None,
    'remote_storage_mode': str(health.get('storage') or 'unknown')[:32] if peer_fresh else 'unknown',
    'remote_thermal': str(health.get('thermal') or 'unknown')[:32] if peer_fresh else 'unknown',
  }
  value['jetlink']['ncm_hello_verified'] = native.get('state') == 'ready' and native.get('transport') == 'ncm-tcp'
  value['observations'] = [reason]
  if not receiver_active:
    value['observations'].append('7000 서버 젯슨 비콘 수신기가 동작하지 않습니다.')
  if recovery.get('state') == 'error' and recovery.get('last_error'):
    value['observations'].append('NCM 복구: '+recovery['last_error'])
  value['notice'] = ('자동 USB 인식과 NCM 복구는 외부 추론을 켜지 않습니다. '
                     'USB carrier/ARP/비콘만으로 원격 서버, HELLO, TensorRT 또는 HUD 화면을 정상 판정하지 않습니다.')
  return value


def report_text(report):
  value = _legacy_report_text(report)
  for section in ('assessment', 'connection_evidence', 'network_recovery', 'remote_boot_and_services', 'remote_service_check'):
    value += '\n['+section+']\n'+json.dumps(report.get(section, {}), ensure_ascii=False, indent=2)+'\n'
  return value
