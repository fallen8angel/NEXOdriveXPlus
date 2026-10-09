"""Real HTTP routes and companion framing with device I/O simulated explicitly."""
import importlib.util
import ast
import asyncio
import json
from pathlib import Path
import struct
import sys
import time
from types import ModuleType, SimpleNamespace

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import pytest

from openpilot.selfdrive.carrot.server.services import jetson as status
from openpilot.tools.jetson import carrot, manage
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout, Message
from openpilot.tools.jetson.transport import protocol as nexd
from openpilot.tools.jetson.retry import UsbRetry

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def api(monkeypatch, tmp_path):
  # Load the real feature with a namespace parent, without importing unrelated
  # Linux-only feature composition. No native cereal/Params modules are mocked.
  package = ModuleType('jetson_route_tests')
  package.__path__ = [str(ROOT / 'openpilot/selfdrive/carrot/server/features')]
  monkeypatch.setitem(sys.modules, package.__name__, package)
  spec = importlib.util.spec_from_file_location('jetson_route_tests.jetson', Path(package.__path__[0]) / 'jetson.py')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, 'CONFIG', tmp_path / 'config.json')
  monkeypatch.delenv('NEXO_JETSON_SSH_TARGET', raising=False)
  monkeypatch.setattr(status, 'DISPLAY_STATUS', tmp_path / 'display.json')
  monkeypatch.setattr(status, 'LINK_STATUS', (tmp_path / 'link.json',))
  monkeypatch.setattr(status, 'MODEL_STATUS', (tmp_path / 'model.json',))
  monkeypatch.setattr(status, 'UDC_ROOT', tmp_path / 'udcs')
  return module


def write(path, record):
  path.write_text(json.dumps(record))


def test_independent_channels_and_disconnect(api):
  state = {'channels': {}}
  receiver = api._JetsonStatusProtocol(state)
  receiver.datagram_received(json.dumps({'magic': api.JETSON_STATUS_MAGIC, 'comma_tcp': True, 'yolo_recent': True}).encode(), ('192.0.2.2', 20))
  receiver.datagram_received(json.dumps({'magic': api.JETSON_STATUS_MAGIC, 'transport': 'usb', 'usb_connected': False}).encode(), ('127.0.0.1', 20))
  now = time.monotonic()
  live = status.snapshot(state, now)
  assert live['connected'] and live['status']['comma_connected']
  assert live['status']['ip'] == '192.0.2.2'
  expired = status.snapshot(state, now + 6)
  assert not expired['connected']
  assert 'yolo_recent' not in expired['status'] and 'ip' not in expired['status']
  receiver.datagram_received(json.dumps({'magic': api.JETSON_STATUS_MAGIC, 'transport': 'usb', 'usb_connected': True}).encode(), ('127.0.0.1', 20))
  state['channels']['wifi']['received_mono'] = now - 6
  again = status.snapshot(state)
  assert again['connected'] and again['status']['usb_connected']
  assert again['status']['ip'] == ''


@pytest.mark.parametrize('raw', [b'[]', b'null', b'{"magic":"NEXO_JETSON_STATUS","x":NaN}', b'x'*65537, b'\xff', b'{}'],
                         ids=['array', 'null', 'nan', 'oversized', 'encoding', 'no_magic'])
def test_bad_beacons_do_not_break_receiver(api, raw):
  state = {'channels': {}}
  api._JetsonStatusProtocol(state).datagram_received(raw, ('192.0.2.2', 2))
  assert state['channels'] == {}


def test_remote_cannot_spoof_local_usb(api):
  state = {'channels': {}}
  api._JetsonStatusProtocol(state).datagram_received(b'{"magic":"NEXO_JETSON_STATUS","transport":"usb","usb_connected":true}', ('192.0.2.2', 2))
  assert state['channels'] == {}


@pytest.mark.parametrize('value', ['recent', {}, [], True, -1])
def test_invalid_frame_age_cannot_break_status(api, value):
  state = {'channels': {}}
  raw = json.dumps({'magic': api.JETSON_STATUS_MAGIC, 'inference_age_s': value}).encode()
  api._JetsonStatusProtocol(state).datagram_received(raw, ('192.0.2.2', 2))
  result = status.snapshot(state)
  assert result['connected'] and result['status']['camera_frame_seen'] is None


def test_native_health_frame_lease_and_clock(api):
  now = time.monotonic()
  write(status.LINK_STATUS[0], {'updated': now, 'state': 'ready', 'last_infer_monotonic': now,
                               'telemetry_updated': now, 'peer': {'carrot_health': {'age_s': .1,
                               'addresses': [{'address': '192.0.2.3'}]}}})
  write(status.MODEL_STATUS[0], {'updated': now, 'ready': True, 'active': True})
  live = status.snapshot({'channels': {}}, now + .2)
  assert live['connected'] and live['status']['camera_frame_seen'] and live['status']['model_active']
  assert live['status']['ip'] == '192.0.2.3'
  assert not status.snapshot({'channels': {}}, now + 4)['connected']
  # Reboot/future timestamps must not stay fresh through max(0, age).
  assert not status.snapshot({'channels': {}}, now - 1)['connected']


def test_ready_file_without_live_peer_is_not_connection(api):
  now = time.monotonic()
  write(status.LINK_STATUS[0], {'updated': now, 'state': 'ready', 'peer': {'ip': '192.0.2.3'},
                               'telemetry_updated': now - 10})
  assert not status.snapshot({'channels': {}}, now)['connected']


@pytest.mark.parametrize(('speed', 'usb3'), [('super-speed', True), ('super-speed-plus', True), ('high-speed', False), ('full-speed', False)])
def test_negotiated_usb_speed(api, tmp_path, speed, usb3):
  controller = tmp_path / 'udcs' / 'a'
  controller.mkdir(parents=True)
  (controller / 'state').write_text('configured')
  (controller / 'current_speed').write_text(speed)
  assert status.usb_status()['usb3'] is usb3


def test_protocol_never_allows_model_or_shutdown_requests():
  wire = carrot.CompanionWire()
  for kind in (3, 5, 6, 8, 17):
    with pytest.raises(nexd.ProtocolError):
      wire.pack_header(kind, 1, 0)
  raw = wire.pack_header(1, 1, 2)
  assert struct.unpack_from('<IH', raw) == (0x4B4E4C4A, 2)
  with pytest.raises(nexd.ProtocolError):
    wire.unpack_header(nexd.pack_header(1, 2, 0))


def test_legacy_protocol_is_preserved():
  wire = carrot.CompanionWire()
  packet = nexd.pack_header(nexd.Msg.HEARTBEAT, 2, 3)
  wire.unpack_header(packet)
  assert wire.mode == 'nexd'
  assert wire.pack_header(nexd.Msg.HUD, 3, 2) == nexd.pack_header(nexd.Msg.HUD, 3, 2)


def test_carrot_session_sends_only_hello_state_and_existing_hud(monkeypatch, tmp_path):
  sent, records = [], []
  responses = [Message(2, 1, 0, memoryview(b'{"protocol":2,"carrot_host":"jetson","carrot_hud_v1":true,"loaded":"abc"}')),
               Message(13, 2, 0, memoryview(b'{"loaded":"abc"}'))]
  transport = SimpleNamespace(send=lambda kind, seq, parts, **kw: sent.append(kind), recv=lambda timeout: responses.pop(0))
  monkeypatch.setattr(carrot, 'STATUS', tmp_path / 'status.json')
  sock = SimpleNamespace(sendto=lambda raw, addr: records.append(json.loads(raw)))
  turns = iter([True, False])
  carrot.session(transport, lambda: next(turns), SimpleNamespace(snapshot=lambda enabled: b'{"version":1}'), sock)
  assert sent == [1, 12, 0x4000]
  assert all(value['usb_connected'] and value['model_ready'] and not value['model_active'] for value in records)


def test_carrot_query_timeout_propagates_to_existing_retry(monkeypatch, tmp_path):
  monkeypatch.setattr(carrot, 'STATUS', tmp_path / 'status.json')
  def fail(timeout):
    raise LinkTimeout('detached')
  with pytest.raises(LinkError):
    carrot.session(SimpleNamespace(send=lambda *a, **kw: None, recv=fail), lambda: True, None, None)


@pytest.fixture
async def http(api):
  app = web.Application()
  api.register(app)
  # Test the real routes without binding the production UDP port.
  app.on_startup.clear()
  server = TestServer(app)
  await server.start_server()
  async with ClientSession() as client:
    yield app, server, client
  await server.close()


async def test_http_no_jetson_pages_and_status(http):
  _, server, client = http
  for route, text in (('/jetson', 'Jetson 설치 가이드'), ('/jetson/install', '원본 설치 가이드 보기')):
    async with client.get(server.make_url(route)) as response:
      assert response.status == 200 and text in await response.text()
  async with client.get(server.make_url('/api/jetson/status')) as response:
    value = await response.json()
    assert value['ok'] and not value['connected'] and not value['can_restart']


async def test_actions_require_csrf_and_parked_server_state(api, http, monkeypatch):
  app, server, client = http
  calls = []
  async def fake(*args):
    calls.append(args)
    return {'ok': True}
  monkeypatch.setattr(api, 'ssh_action', fake)
  write(api.CONFIG, {'ssh_target': 'configured-jetson'})
  headers = {'X-Jetson-Token': app['jetson_status_state']['csrf']}
  async with client.post(server.make_url('/api/jetson/action'), json={'action': 'restart', 'confirm': True}) as response:
    assert response.status == 403
  async with client.post(server.make_url('/api/jetson/action'), json={'action': 'restart', 'confirm': True}, headers=headers) as response:
    assert response.status == 409
  app['params'] = SimpleNamespace(get_bool=lambda key: key == 'IsOffroad')
  async with client.post(server.make_url('/api/jetson/action'), json={'action': 'restart'}, headers=headers) as response:
    assert response.status == 409
  async with client.post(server.make_url('/api/jetson/action'), json={'action': 'restart', 'confirm': True}, headers=headers) as response:
    assert response.status == 200
  assert calls == [('configured-jetson', 'restart', True)]


@pytest.mark.parametrize('name', ['erase', 'setup', 'update', 'poweroff', 'shutdown', 'shell', 'reboot'])
async def test_dangerous_or_unknown_commands_have_no_route(http, name):
  app, server, client = http
  async with client.post(server.make_url('/api/jetson/action'), json={'action': name},
                         headers={'X-Jetson-Token': app['jetson_status_state']['csrf']}) as response:
    assert response.status == 400


@pytest.mark.parametrize('target', ['-oProxyCommand=bad', 'user@host;id', 'user@host\n', 'user@host/../../', 'a b'])
async def test_ssh_target_is_not_shell_input(http, target):
  app, server, client = http
  async with client.post(server.make_url('/api/jetson/action'), json={'action': 'configure', 'ssh_target': target},
                         headers={'X-Jetson-Token': app['jetson_status_state']['csrf']}) as response:
    assert response.status == 400


async def test_cross_origin_action_rejected(http):
  app, server, client = http
  async with client.post(server.make_url('/api/jetson/action'), json={'action': 'diagnose'},
                         headers={'X-Jetson-Token': app['jetson_status_state']['csrf'], 'Origin': 'https://example.invalid'}) as response:
    assert response.status == 403


async def test_receiver_bind_failure_is_optional(api, monkeypatch):
  loop = SimpleNamespace(create_datagram_endpoint=None)
  async def fail(*args, **kwargs):
    raise OSError('port occupied')
  loop.create_datagram_endpoint = fail
  monkeypatch.setattr(api.asyncio, 'get_running_loop', lambda: loop)
  app = {'jetson_status_state': {}}
  await api._start_receiver(app)
  assert 'port occupied' in app['jetson_status_state']['error']


def test_helper_refuses_non_jetson_and_unknown_action():
  with pytest.raises(ValueError):
    manage.handle('erase')
  if sys.platform == 'win32':
    with pytest.raises(ValueError, match='not a Jetson'):
      manage.handle('restart')


def test_required_helper_is_real(api):
  assert api.HELPER.is_file()


def test_bounded_retry_recovers_after_device_reboot_in_same_car_boot(tmp_path):
  path = tmp_path / 'retry.json'
  retry = UsbRetry(path, 'same-car-boot')
  retry.recover_after = 30.
  for now in (10, 11, 13):
    retry.begin(now)
    retry.failed(now)
  restarted = UsbRetry(path, 'same-car-boot')
  restarted.recover_after = 30.
  assert not restarted.ready(42.9)
  assert restarted.ready(43)
  restarted.begin(43)
  restarted.observe(True, 43)
  restarted.observe(True, 45.1)
  assert restarted.failures == 0


def test_native_poll_failure_exits_optional_session_without_ready_loop():
  # Exercise the actual function AST. Native imports require Linux builds, but
  # this session function only needs its explicit I/O and publishing dependencies.
  path = ROOT / 'openpilot/selfdrive/modeld/jetlink/daemon.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'serve_modeld')
  function.args.args[-1].annotation = None
  records = []
  class Listener:
    def accept(self):
      raise TimeoutError
  class Client:
    last_state = {}
    def state(self, timeout):
      raise ConnectionError('Jetson rebooted')
  namespace = {'time': time, 'enabled': lambda: True, 'publish': lambda *a, **kw: records.append(a)}
  exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
  with pytest.raises(ConnectionError, match='rebooted'):
    namespace['serve_modeld'](Listener(), Client(), None)
  assert records == [('ready', None)]


async def test_ssh_uses_fixed_script_and_collects_chunked_output(api, monkeypatch):
  output = asyncio.StreamReader()
  error = asyncio.StreamReader()
  output.feed_data(b'{"ok":')
  output.feed_data(b'true}')
  output.feed_eof()
  error.feed_eof()
  sent, arguments = [], []
  async def drain():
    pass
  async def wait():
    proc.returncode = 0
  proc = SimpleNamespace(stdin=SimpleNamespace(write=sent.append, drain=drain, close=lambda: None),
                         stdout=output, stderr=error, wait=wait, returncode=None)
  async def create(*args, **kwargs):
    arguments.extend(args)
    return proc
  monkeypatch.setattr(api.asyncio, 'create_subprocess_exec', create)
  assert await api.ssh_action('registered-jetson', 'status') == {'ok': True}
  assert 'StrictHostKeyChecking=yes' in arguments and 'BatchMode=yes' in arguments
  assert arguments[-4:] == ['registered-jetson', 'python3', '-', 'status']
  assert sent == [api.HELPER.read_bytes()]


def test_helper_detects_service_names_and_redacts_logs(monkeypatch):
  def run(args, timeout=3):
    if args[0] == 'journalctl':
      return {'code': 0, 'output': 'service ready\npassword=secret\nframe received'}
    unit = args[2]
    if unit == 'carrot-jetlink.service':
      return {'code': 0, 'output': 'LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nWorkingDirectory=/opt/runtime/carrot'}
    return {'code': 0, 'output': 'LoadState=not-found'}
  monkeypatch.setattr(manage, 'run', run)
  original = Path.is_file
  monkeypatch.setattr(Path, 'is_file', lambda path: True if path.as_posix() == '/etc/nv_tegra_release' else original(path))
  assert list(manage.services()) == ['carrot-jetlink.service']
  logs = manage.handle('logs')
  assert logs['ok'] and 'frame received' in logs['logs'] and 'secret' not in logs['logs']


def test_helper_restart_needs_confirmation_and_propagates_permission_failure(monkeypatch):
  original = Path.is_file
  monkeypatch.setattr(Path, 'is_file', lambda path: True if path.as_posix() == '/etc/nv_tegra_release' else original(path))
  monkeypatch.setattr(manage, 'services', lambda: {'carrot-jetlink.service': {'ActiveState': 'active'}})
  monkeypatch.setattr(sys, 'argv', ['manage', 'restart'])
  with pytest.raises(ValueError, match='confirmation'):
    manage.handle('restart')
  monkeypatch.setattr(sys, 'argv', ['manage', 'restart', '--confirmed-offroad'])
  commands = []
  def fail(args, timeout):
    commands.append(args)
    return {'code': 1, 'output': 'sudo permission required'}
  monkeypatch.setattr(manage, 'run', fail)
  with pytest.raises(RuntimeError, match='sudo permission'):
    manage.handle('restart')
  assert commands == [['sudo', '-n', 'systemctl', 'restart', 'carrot-jetlink.service']]


def test_update_check_never_applies_unsigned_manifest(monkeypatch, tmp_path):
  raw = json.dumps({'format': 1, 'source_commit': 'a'*40}).encode()
  class Response:
    url = manage.MANIFEST_URL
    def __enter__(self):
      return self
    def __exit__(self, *args):
      pass
    def read(self, limit):
      return raw
  monkeypatch.setattr(manage.urllib.request, 'urlopen', lambda *a, **kw: Response())
  result = manage.latest_carrot(tmp_path, {})
  assert not result['signature_verified'] and result['available'] is None and not result['applied']
  assert not list(tmp_path.iterdir())
