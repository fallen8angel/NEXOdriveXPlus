"""Read-only USB-NCM and Jetlink troubleshooting for the port-7000 web UI.

Never opens a Jetlink TCP session, changes a USB gadget, or configures routes.
Peer state is unknown unless fresh independent telemetry proves it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm


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
  return {'udc_bound': bool(bound), 'controllers': controllers, 'ncm_interfaces': interfaces,
          'ncm_interface': iface, 'comma_ipv4': address, 'expected_comma_ipv4': ncm.COMMA_ADDR,
          'expected_jetson_ipv4': ncm.JETSON_ADDR, 'peer_route_via_usb': route}


def build_report(result, local, receiver_active=False):
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
    'git': {'branch': _git_value('rev-parse', '--abbrev-ref', 'HEAD'),
            'commit': _git_value('rev-parse', 'HEAD')},
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
