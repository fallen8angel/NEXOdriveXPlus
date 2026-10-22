"""7000 companion management. Failure remains local to this optional feature."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from openpilot.selfdrive.carrot.server.services.jetson import address, read_record, snapshot
from openpilot.selfdrive.carrot.server.services.jetson_diagnostics import build_report, report_text, connection_evidence
from openpilot.selfdrive.carrot.server.services.jetson_observer import LinkHistory, history_text
from openpilot.tools.jetson.state import decode_json
from openpilot.tools.jetson.manage import ACTIONS
from openpilot.selfdrive.modeld.jetlink.network import usb0_state
from .jetson_pages import GUIDE, PAGE

JETSON_STATUS_PORT = 8766
JETSON_STATUS_MAGIC = 'NEXO_JETSON_STATUS'
JETLINK_STATUS_PORT = 5600
CONFIG = Path('/data/carrot/jetson-management.json')
HELPER = Path(__file__).resolve().parents[4] / 'tools/jetson/manage.py'
JETLINK_ENABLED = Path('/data/nexo_jetlink_enabled')
_TARGET = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,127}\Z')


class _JetsonStatusProtocol(asyncio.DatagramProtocol):
  def __init__(self, state):
    self.state = state

  def datagram_received(self, data, addr):
    try:
      value = decode_json(data, 64 * 1024)
    except (ValueError, UnicodeError, RecursionError):
      return
    if value.get('magic') != JETSON_STATUS_MAGIC:
      return
    channel = value.get('transport', 'wifi')
    if channel not in ('usb', 'wifi'):
      return
    # USB forwarding is local; a LAN sender cannot override its independent lease.
    source = str(addr[0]) if addr else ''
    if channel == 'usb' and source != '127.0.0.1':
      return
    previous = self.state['channels'].get(channel, {})
    self.state['channels'][channel] = {'packet': value, 'source_ip': source, 'received_mono': time.monotonic(),
                                       'packets': previous.get('packets', 0) + 1}

  def error_received(self, error):
    self.state['error'] = str(error)[:240]

  def connection_lost(self, error):
    self.state['transport'] = None
    if error:
      self.state['error'] = str(error)[:240]


async def _bind_receiver(app):
  state = app['jetson_status_state']
  if state.get('transport') is not None:
    return
  try:
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
      lambda: _JetsonStatusProtocol(state), local_addr=('0.0.0.0', JETSON_STATUS_PORT))
    state['transport'] = transport
    state['error'] = ''
  except Exception as error:
    state['error'] = f'상태 수신기 사용 불가: {error}'[:240]


async def _receiver_loop(app):
  while True:
    await asyncio.sleep(2)
    await _bind_receiver(app)


async def _start_receiver(app):
  state = app['jetson_status_state']
  if state.get('receiver_task') is not None:
    return
  state['receiver_task'] = asyncio.create_task(_receiver_loop(app))
  await _bind_receiver(app)


async def _observe_link(app):
  """Six-second passive poll, decoupled from HTTP refresh and modeld."""
  state = app['jetson_status_state']
  while True:
    try:
      report = await _local_diagnostic_report(app, include_git=False)
      state['observer_report'] = report
      state['observer_checked_mono'] = time.monotonic()
      await asyncio.to_thread(state['link_history'].observe, report)
      state['observer_error'] = ''
    except asyncio.CancelledError:
      raise
    except Exception as error:
      state['observer_error'] = f'{type(error).__name__}: passive observation unavailable'[:120]
    await asyncio.sleep(6)


async def _start_observer(app):
  app['jetson_status_state']['observer_task'] = asyncio.create_task(_observe_link(app))


async def _stop_observer(app):
  task = app['jetson_status_state'].pop('observer_task', None)
  if task is not None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
      await task


async def _stop_receiver(app):
  state = app['jetson_status_state']
  task = state.pop('receiver_task', None)
  if task is not None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
      await task
  if state.get('transport'):
    state['transport'].close()
    state['transport'] = None


def remote_service_observation(state):
  """Fresh, bounded SSH status obtained by an explicit user click in port 7000."""
  cached = state.get('verified_remote_services')
  age = time.monotonic() - cached['received_mono'] if isinstance(cached, dict) else float('inf')
  if age < 0 or age >= 60:
    return {'fresh': False, 'services': {}, 'age_s': None}
  return {'fresh': True, 'services': dict(cached['services']), 'age_s': round(age, 1)}


def beacon_observation(state):
  now = time.monotonic()
  return {channel: {'packets': entry.get('packets', 0), 'age_s': now - entry['received_mono'],
                    'source_ip': entry.get('source_ip', '')}
          for channel, entry in state['channels'].items()
          if channel in ('usb', 'wifi') and 0 <= now - entry['received_mono']}


async def _probe_jetlink(state, ip):
  ip = address(ip)
  previous = state.get('jetlink_probe', {})
  now = time.monotonic()
  if ip and previous.get('ip') == ip and 0 <= now - previous.get('checked_mono', 0) < 3:
    return previous
  result = {'checked': bool(ip), 'reachable': False, 'ip': ip, 'port': JETLINK_STATUS_PORT,
            'url': '', 'error': '', 'checked_mono': now}
  if ip:
    host = f'[{ip}]' if ':' in ip else ip
    url = f'http://{host}:{JETLINK_STATUS_PORT}'
    try:
      # An open TCP port is not proof that Jetlink has a working web page.
      async with ClientSession(timeout=ClientTimeout(total=.6), trust_env=False) as session:
        async with session.get(url, allow_redirects=False) as response:
          result['reachable'] = response.status in (200, 401, 403) and 'text/html' in response.headers.get('Content-Type', '')
      if result['reachable']:
        result['url'] = url
    except Exception as error:
      result['error'] = str(error)[:160]
  state['jetlink_probe'] = result
  return result


async def _probe_display_usb(local):
  """Passive observation: status polling must not occupy the single TCP server."""
  interface_up = local.get('usb_admin_up') is True and local.get('usb_carrier') is True
  observation = {'usb_link': interface_up, 'display_enabled': local.get('display_usb') is True,
                 'server_reachable': False, 'hud_usb_detected': None,
                 'hud_output_verified': False, 'detail': '실제 HUD USB 인식·화면 출력은 Jetson 상태 보고가 필요합니다'}
  if not interface_up:
    observation['detail'] = 'comma USB 링크 미확인'
    return observation
  if local.get('ncm_session_verified') is True:
    observation['server_reachable'] = True
    observation['detail'] = 'NCM 세션 응답 확인 · 실제 화면 출력 미검증'
  else:
    observation['detail'] = 'USB 링크 정상 · TCP 세션 확인 대기 (상태 조회는 서버에 접속하지 않음)'
  return observation


def target():
  value = str(os.environ.get('NEXO_JETSON_SSH_TARGET') or read_record(CONFIG).get('ssh_target') or '')
  return value if _TARGET.fullmatch(value) else ''


def _param_bool(app, name):
  params = app.get('params')
  if params is None:
    return None
  try:
    return bool(params.get_bool(name))
  except Exception:
    return None


def _local_jetlink_state(app, result):
  try:
    enabled = JETLINK_ENABLED.exists()
  except OSError:
    enabled = False
  display_usb = _param_bool(app, 'NexoJetsonUsb')
  onroad = _param_bool(app, 'IsOnroad')
  offroad = _param_bool(app, 'IsOffroad')
  # IsOnroad=0 is sufficient to describe the local parked/offroad wait state
  # even on forks that do not keep IsOffroad populated continuously.
  if onroad is False:
    offroad = True
  native = result.get('native_link') if isinstance(result.get('native_link'), dict) else {}
  native_state = str(native.get('state') or 'unavailable')
  daemon_active = native_state not in ('', 'unavailable', 'stopped')
  conflict = enabled and display_usb is True
  waiting_modeld = enabled and not conflict and offroad is True and not daemon_active
  usb = usb0_state()

  if conflict:
    summary = 'Jetlink 충돌 · NexoJetsonUsb 표시 모드를 꺼야 합니다'
    mode = 'conflict'
  elif waiting_modeld:
    summary = 'Jetlink 활성화됨 · 오프로드에서 modeld 시작 대기'
    mode = 'waiting_modeld'
  elif enabled and daemon_active and not result.get('connected'):
    summary = f'Jetlink 실행 중 · {native_state} · Jetson 응답 대기'
    mode = 'connecting'
  elif enabled and result.get('connected'):
    summary = 'Jetlink 활성화됨 · Jetson 상태 수신 중'
    mode = 'active'
  elif enabled:
    summary = 'Jetlink 활성화됨 · 연결 확인 중'
    mode = 'enabled'
  elif display_usb is True:
    summary = '기존 NexoJetsonUsb 표시 모드 사용 중'
    mode = 'display_usb'
  else:
    summary = 'Jetlink 비활성 · 로컬 modeld 사용'
    mode = 'disabled'

  return {'enabled': enabled, 'display_usb': display_usb, 'onroad': onroad, 'offroad': offroad,
          'ncm_session_verified': result.get('connected') is True and (
            result.get('status', {}).get('protocol') == 'nexo-ncm-display-v1'
            or native_state == 'ready' and native.get('transport') == 'ncm-tcp'),
          **usb, 'native_state': native_state, 'daemon_active': daemon_active,
          'waiting_modeld': waiting_modeld, 'conflict': conflict, 'mode': mode, 'summary': summary}


def _set_stage(result, stage_id, state, detail):
  for stage in result.get('stages', []):
    if stage.get('id') == stage_id:
      stage.update(state=state, detail=detail)
      return


def _apply_local_jetlink_context(result, local):
  status = dict(result.get('status') or {})
  result['status'] = status
  result['local_jetlink'] = local
  status.update({key: value for key, value in local.items() if key.startswith('usb_')})

  if local['conflict']:
    result['state'] = '오류'
    result['diagnosis'] = local['summary']
    status['last_error'] = local['summary']
    _set_stage(result, 'jetlink', 'ERROR', local['summary'])
    return

  if not local['enabled']:
    # Network liveness is independent of the inference opt-in. It does not
    # prove a Jetson application, camera frame, engine, or physical HUD output.
    if local['usb_admin_up'] is True and local['usb_carrier'] is True:
      status['usb_connected'] = True
      _set_stage(result, 'usb', 'OK', '물리 연결됨 · 추론 활성화와 별개')
    elif local['usb_admin_up'] is False or local['usb_carrier'] is False:
      status['usb_connected'] = False
      _set_stage(result, 'usb', 'DISCONNECTED', 'USB 링크 미연결')
    if not result.get('connected'):
      result['diagnosis'] = local['summary'] + ' · USB 연결만으로 Jetlink 서버 응답을 확인할 수 없습니다'
      _set_stage(result, 'jetlink', 'WAITING', '추론 OFF · TCP/HELLO 세션 미확인')
      _set_stage(result, 'camera', 'WAITING', '추론 OFF · 카메라 전송 미실행')
      _set_stage(result, 'inference', 'WAITING', 'OFF · 로컬 모델 유지')
    return

  # Keep native faults/progress independent of a still-live YOLO Wi-Fi beacon.
  # In particular, never rewrite a g1 ownership error into WAITING below.
  native = result.get('native_link', {})
  if native.get('severity') in ('error', 'loading'):
    return

  carrier = local['usb_carrier']
  if local['usb_admin_up'] is False:
    status['usb_connected'] = False
    _set_stage(result, 'usb', 'WAITING', 'USB 인터페이스 DOWN')
  elif result.get('connected'):
    return
  elif carrier is True:
    status['usb_connected'] = True
    _set_stage(result, 'usb', 'OK', '물리 연결됨 · Jetson 응답 대기')
  elif carrier is False:
    status['usb_connected'] = False
    _set_stage(result, 'usb', 'WAITING', 'USB 물리 연결 확인 필요')
  else:
    _set_stage(result, 'usb', 'WAITING', 'USB carrier 확인 대기')

  if result.get('connected'):
    return

  if local['waiting_modeld']:
    result['state'] = '대기'
    result['diagnosis'] = local['summary']
    status.update(service_active=False, camera_frame_seen=None, model_active=None, model_ready=None,
                  pipeline='NEXO Jetlink · 추론 USB · modeld 시작 대기', protocol='carrot-v2')
    _set_stage(result, 'jetson', 'WAITING', 'Jetlink 활성화됨 · Jetson 상태 신호 대기')
    _set_stage(result, 'jetlink', 'WAITING', '활성화됨 · modeld 시작 대기')
    _set_stage(result, 'camera', 'WAITING', 'modeld 시작 후 확인')
    _set_stage(result, 'inference', 'WAITING', 'modeld 시작 후 확인')
    _set_stage(result, 'hud', 'WAITING', 'Jetlink 연결 후 확인')
    return

  if result.get('state') == '미연결':
    result['state'] = '연결 중'
  if not status.get('last_error'):
    result['diagnosis'] = local['summary']
  _set_stage(result, 'jetson', 'WAITING', 'Jetson heartbeat 대기')
  _set_stage(result, 'jetlink', 'WAITING', f"활성화됨 · {local['native_state']}")


def parked(app):
  params = app.get('params')
  try:
    if params is None or not params.get_bool('IsOffroad') or params.get_bool('IsOnroad'):
      return False
  except Exception:
    return False
  current = snapshot(app['jetson_status_state'])
  # Do not interrupt active or uncertain inference ownership.
  native = current['native_link']
  if JETLINK_ENABLED.exists() and native.get('model_active') is not False:
    return False
  return native.get('model_active') is not True


async def api_jetson_status(request):
  state = request.app['jetson_status_state']
  result = snapshot(state)
  local = _local_jetlink_state(request.app, result)
  _apply_local_jetlink_context(result, local)
  checked = state.get('observer_checked_mono')
  age = time.monotonic() - checked if type(checked) in (float, int) else None
  # A previous result cannot indefinitely assert a present USB gadget.
  observer_fresh = age is not None and 0 <= age < 18
  observer = state.get('observer_report') or {} if observer_fresh else {}
  result['connection_evidence'] = connection_evidence(result, local, observer.get('usb_gadget') or {})
  result['connection_evidence']['observer_age_s'] = round(age, 1) if observer_fresh else None
  result['connection_evidence']['observer_fresh'] = observer_fresh
  result['display_observation'] = await _probe_display_usb(local)
  result.update(ok=True, receiver_active=state.get('transport') is not None, receiver_error=state.get('error', ''),
                beacons=beacon_observation(state),
                csrf=state['csrf'], ssh_target=target(), can_restart=parked(request.app),
                management_busy=state['action_lock'].locked(),
                jetlink=await _probe_jetlink(state, result['status'].get('ip', '')),
                observer_summary=observer.get('assessment'),
                auto_detection=observer.get('usb_gadget'),
                remote_boot_and_services=observer.get('remote_boot_and_services'),
                network_recovery=observer.get('network_recovery'),
                observer_error=state.get('observer_error', ''),
                remote_service_check=remote_service_observation(state))
  return web.json_response(result, headers={'Cache-Control': 'no-store'})


async def _local_diagnostic_report(app, include_git=True):
  state = app['jetson_status_state']
  result = snapshot(state)
  local = _local_jetlink_state(app, result)
  _apply_local_jetlink_context(result, local)
  # sysfs and bounded read-only 'ip'/'git' commands run outside the event loop.
  return await asyncio.to_thread(build_report, result, local, state.get('transport') is not None, include_git)


async def api_jetson_diagnostics(request):
  report = await _local_diagnostic_report(request.app)
  report['remote_service_check'] = remote_service_observation(request.app['jetson_status_state'])
  export = request.query.get('format', '')
  if export not in ('', 'json', 'txt'):
    raise web.HTTPBadRequest(text='unsupported format')
  headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
  if export:
    headers['Content-Disposition'] = 'attachment; filename="nexo-jetson-diagnostic.' + export + '"'
  if export == 'txt':
    return web.Response(text=report_text(report), content_type='text/plain', charset='utf-8', headers=headers)
  return web.json_response(report, headers=headers)


async def api_jetson_history(request):
  state = request.app['jetson_status_state']
  result = state['link_history'].export()
  export = request.query.get('format', '')
  if export not in ('', 'json', 'txt'):
    raise web.HTTPBadRequest(text='unsupported format')
  headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
  if export:
    headers['Content-Disposition'] = 'attachment; filename="nexo-jetson-history.' + export + '"'
  if export == 'txt':
    return web.Response(text=history_text(result), content_type='text/plain', charset='utf-8', headers=headers)
  return web.json_response(result, headers=headers)


def check_request(request):
  origin = request.headers.get('Origin')
  if origin and urlsplit(origin).netloc != request.host:
    raise web.HTTPForbidden(text='same-origin required')
  if request.content_type != 'application/json':
    raise web.HTTPUnsupportedMediaType(text='JSON required')
  if not secrets.compare_digest(request.headers.get('X-Jetson-Token', ''), request.app['jetson_status_state']['csrf']):
    raise web.HTTPForbidden(text='Jetson confirmation token required')


async def ssh_action(ssh_target, action, confirmed=False):
  if not _TARGET.fullmatch(ssh_target) or action not in ACTIONS:
    return {'ok': False, 'error': 'SSH 대상 또는 명령이 잘못되었습니다.'}
  script = HELPER.read_bytes()
  args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=3',
          '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2', '--', ssh_target, 'python3', '-', action]
  if confirmed:
    args.append('--confirmed-offroad')
  proc = None
  try:
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536)

    async def exchange():
      proc.stdin.write(script)
      await proc.stdin.drain()
      proc.stdin.close()
      async def bounded(stream, limit):
        chunks = []
        size = 0
        while chunk := await stream.read(min(8192, limit + 1 - size)):
          size += len(chunk)
          if size > limit:
            raise ValueError('Jetson response exceeded limit')
          chunks.append(chunk)
        return b''.join(chunks)
      out, err = await asyncio.gather(bounded(proc.stdout, 65536), bounded(proc.stderr, 8192))
      await proc.wait()
      if proc.returncode != 0:
        raise ValueError(err.decode(errors='replace')[-1000:] or 'SSH command failed')
      return decode_json(out, 65536)

    return await asyncio.wait_for(exchange(), timeout=15)
  except (OSError, ValueError, TimeoutError) as error:
    return {'ok': False, 'error': f'{type(error).__name__}: {error}'[:1000]}
  finally:
    if proc is not None and proc.returncode is None:
      proc.kill()
      await proc.wait()


async def api_jetson_action(request):
  check_request(request)
  try:
    raw = await request.content.read(4097)
    body = decode_json(raw, 4096)
  except (ValueError, UnicodeError):
    raise web.HTTPBadRequest(text='invalid request') from None
  action = body.get('action')
  if action not in (*ACTIONS, 'configure', 'diagnose'):
    raise web.HTTPBadRequest(text='unsupported action')
  state = request.app['jetson_status_state']
  if action == 'configure':
    value = body.get('ssh_target', '')
    if not isinstance(value, str) or (value and not _TARGET.fullmatch(value)):
      raise web.HTTPBadRequest(text='SSH alias or user@host required')
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix('.tmp')
    temporary.write_text(json.dumps({'ssh_target': value}), encoding='utf-8')
    temporary.replace(CONFIG)
    return web.json_response({'ok': True, 'note': 'SSH 대상 저장 완료 · 키와 known_hosts는 기존 SSH 설정을 사용합니다.'})
  if action == 'diagnose':
    result = snapshot(state)
    local = _local_jetlink_state(request.app, result)
    _apply_local_jetlink_context(result, local)
    result['receiver_error'] = state.get('error', '')
    report = await _local_diagnostic_report(request.app)
    return web.json_response({'ok': True, 'diagnostics': result, 'local_usb_diagnostic': report})
  disruptive = action in ('restart', 'reconnect')
  if disruptive and (body.get('confirm') is not True or not parked(request.app)):
    raise web.HTTPConflict(text='정차 및 비활성 모델 상태와 명시적 확인이 필요합니다.')
  ssh_target = target()
  if not ssh_target:
    return web.json_response({'ok': False, 'error': 'SSH 대상을 먼저 설정하고 공개키와 호스트 키를 등록해 주세요.'}, status=409)
  if state['action_lock'].locked():
    raise web.HTTPConflict(text='Jetson 명령 실행 중입니다.')
  async with state['action_lock']:
    # Recheck after serialization; an earlier status page is not authorization.
    if disruptive and not parked(request.app):
      raise web.HTTPConflict(text='정차 상태를 다시 확인하지 못했습니다.')
    result = await ssh_action(ssh_target, action, disruptive)
    if result.get('ok'):
      remote = result.get('status', result.get('after', {}))
      if action == 'status' and isinstance(remote, dict):
        # An explicit authenticated management check: TTL prevents stale
        # service data from being confused with current Jetson liveness.
        units = remote.get('services')
        if isinstance(units, dict):
          allow = ('carrot-jetlink.service', 'carrot-jetlink-hud.service',
                   'nexo-jetlink-server.service', 'nexo-jetson-hud.service',
                   'nexo-jetson-host.service', 'nexo-yolo.service')
          state['verified_remote_services'] = {
            'received_mono': time.monotonic(),
            'services': {unit: str(units[unit].get('ActiveState') or 'unknown')[:24]
                         for unit in allow if isinstance(units.get(unit), dict)}}
        else:
          state.pop('verified_remote_services', None)
      summary = remote.get('summary') if isinstance(remote, dict) else None
      if isinstance(summary, dict):
        state['channels']['ssh'] = {'packet': summary, 'source_ip': '', 'received_mono': time.monotonic()}
    else:
      state['channels'].pop('ssh', None)
      if action == 'status':
        state.pop('verified_remote_services', None)
  return web.json_response(result, headers={'Cache-Control': 'no-store'})


async def jetson_page(_request):
  return web.Response(text=PAGE, content_type='text/html', headers={'Cache-Control': 'no-store'})


async def jetson_guide(_request):
  return web.Response(text=GUIDE, content_type='text/html')


def register(app):
  app['jetson_status_state'] = {'channels': {}, 'transport': None, 'error': '', 'jetlink_probe': {},
                              'csrf': secrets.token_hex(32), 'action_lock': asyncio.Lock(),
                              'link_history': LinkHistory(), 'observer_report': None, 'observer_error': ''}
  app.on_startup.append(_start_receiver)
  app.on_startup.append(_start_observer)
  app.on_cleanup.append(_stop_observer)
  app.on_cleanup.append(_stop_receiver)
  app.router.add_get('/api/jetson/status', api_jetson_status)
  app.router.add_get('/api/jetson/diagnostics', api_jetson_diagnostics)
  app.router.add_get('/api/jetson/history', api_jetson_history)
  app.router.add_post('/api/jetson/action', api_jetson_action)
  app.router.add_get('/jetson', jetson_page)
  app.router.add_get('/jetson/install', jetson_guide)
