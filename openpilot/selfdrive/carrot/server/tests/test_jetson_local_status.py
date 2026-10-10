from pathlib import Path
import errno
import importlib.util
import sys
from types import ModuleType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.selfdrive.modeld.jetlink import network


@pytest.fixture
def jetson(monkeypatch):
  # Import the real optional route without Linux-only feature composition.
  package = ModuleType('jetson_local_status_tests')
  package.__path__ = [str(Path(__file__).resolve().parents[1] / 'features')]
  monkeypatch.setitem(sys.modules, package.__name__, package)
  spec = importlib.util.spec_from_file_location(f'{package.__name__}.jetson', Path(package.__path__[0]) / 'jetson.py')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class FakeParams:
  def __init__(self, values):
    self.values = values

  def get_bool(self, name):
    return self.values.get(name, False)


def base_result():
  return {
    'connected': False,
    'state': '미연결',
    'status': {'state': '미연결', 'last_error': '', 'usb_speed': '미연결', 'usb3': None},
    'native_link': {'state': 'unavailable', 'model': '', 'model_ready': None, 'model_active': None},
    'stages': [
      {'id': 'jetson', 'label': 'Jetson', 'state': 'DISCONNECTED', 'detail': ''},
      {'id': 'usb', 'label': 'USB', 'state': 'DISCONNECTED', 'detail': ''},
      {'id': 'jetlink', 'label': 'Jetlink', 'state': 'DISCONNECTED', 'detail': ''},
      {'id': 'camera', 'label': 'Camera', 'state': 'DISCONNECTED', 'detail': ''},
      {'id': 'inference', 'label': 'Inference', 'state': 'DISCONNECTED', 'detail': ''},
      {'id': 'hud', 'label': 'HUD', 'state': 'DISCONNECTED', 'detail': ''},
    ],
  }


def stage_map(result):
  return {stage['id']: stage for stage in result['stages']}


def configure_paths(tmp_path, monkeypatch, jetson, carrier='1', flags='0x10001', operstate='up'):
  marker = tmp_path / 'nexo_jetlink_enabled'
  marker.touch()
  usb = tmp_path / 'usb0'
  usb.mkdir()
  (usb / 'carrier').write_text(carrier, encoding='utf-8')
  (usb / 'flags').write_text(flags, encoding='utf-8')
  (usb / 'operstate').write_text(operstate, encoding='utf-8')
  monkeypatch.setattr(jetson, 'JETLINK_ENABLED', marker)
  monkeypatch.setattr(network, 'USB0', usb)


def test_enabled_offroad_reports_modeld_wait_instead_of_disconnected(tmp_path, monkeypatch, jetson):
  configure_paths(tmp_path, monkeypatch, jetson)
  app = {'params': FakeParams({'IsOnroad': False, 'IsOffroad': True, 'NexoJetsonUsb': False})}
  result = base_result()

  local = jetson._local_jetlink_state(app, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['waiting_modeld'] is True
  assert local['usb_carrier'] is True
  assert result['state'] == '대기'
  assert 'modeld 시작 대기' in result['diagnosis']
  assert result['status']['usb_connected'] is True
  assert result['status']['service_active'] is False
  assert 'NEXO Jetlink' in result['status']['pipeline']
  stages = stage_map(result)
  assert stages['usb']['state'] == 'OK'
  assert stages['usb']['detail'] == '물리 연결됨 · Jetson 응답 대기'
  assert local['usb_admin_state'] == 'up'
  assert local['usb_operstate'] == 'up'
  assert stages['jetlink']['state'] == 'WAITING'
  assert stages['camera']['state'] == 'WAITING'
  assert stages['inference']['state'] == 'WAITING'
  assert stages['hud']['state'] == 'WAITING'


def test_enabled_display_usb_conflict_is_explicit(tmp_path, monkeypatch, jetson):
  configure_paths(tmp_path, monkeypatch, jetson)
  app = {'params': FakeParams({'IsOnroad': False, 'IsOffroad': True, 'NexoJetsonUsb': True})}
  result = base_result()

  local = jetson._local_jetlink_state(app, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['conflict'] is True
  assert result['state'] == '오류'
  assert 'NexoJetsonUsb' in result['status']['last_error']
  assert stage_map(result)['jetlink']['state'] == 'ERROR'


def test_disabled_mode_keeps_disconnected_snapshot_unchanged(tmp_path, monkeypatch, jetson):
  marker = tmp_path / 'missing_marker'
  carrier_path = tmp_path / 'carrier'
  carrier_path.write_text('1', encoding='utf-8')
  monkeypatch.setattr(jetson, 'JETLINK_ENABLED', marker)
  monkeypatch.setattr(network, 'USB0', tmp_path)
  app = {'params': FakeParams({'IsOnroad': False, 'IsOffroad': True, 'NexoJetsonUsb': False})}
  result = base_result()

  local = jetson._local_jetlink_state(app, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['enabled'] is False
  assert result['state'] == '미연결'
  assert stage_map(result)['jetlink']['state'] == 'DISCONNECTED'


def test_admin_down_with_einval_is_explicit(tmp_path, monkeypatch, jetson):
  configure_paths(tmp_path, monkeypatch, jetson, flags='0x1002', operstate='down')
  original = Path.read_text

  def read(path, *args, **kwargs):
    if path == network.USB0 / 'carrier':
      raise OSError(errno.EINVAL, 'Invalid argument')
    return original(path, *args, **kwargs)

  monkeypatch.setattr(Path, 'read_text', read)
  app = {'params': FakeParams({'IsOffroad': True})}
  result = base_result()
  local = jetson._local_jetlink_state(app, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['usb_admin_state'] == local['usb_operstate'] == 'down'
  assert local['usb_carrier'] is False
  assert local['usb_carrier_source'] == 'admin_down'
  assert 'Invalid argument' in local['usb_carrier_error']
  assert result['status']['usb_connected'] is False
  assert stage_map(result)['usb']['detail'] == 'USB 인터페이스 DOWN'
  assert result['state'] == '대기'


def test_admin_up_without_carrier_is_physical_wait(tmp_path, monkeypatch, jetson):
  configure_paths(tmp_path, monkeypatch, jetson, carrier='', flags='0x1003', operstate='down')
  result = base_result()
  local = jetson._local_jetlink_state({'params': FakeParams({'IsOffroad': True})}, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['usb_admin_up'] is True
  assert local['usb_operstate'] == 'down'
  assert local['usb_carrier'] is False
  assert stage_map(result)['usb']['detail'] == 'USB 물리 연결 확인 필요'


def test_admin_down_is_visible_even_with_live_wifi_heartbeat(tmp_path, monkeypatch, jetson):
  configure_paths(tmp_path, monkeypatch, jetson, carrier='', flags='0x1002', operstate='down')
  result = base_result()
  result.update(connected=True, state='연결됨')
  local = jetson._local_jetlink_state({'params': FakeParams({'IsOffroad': True})}, result)
  jetson._apply_local_jetlink_context(result, local)

  assert result['connected'] is True
  assert result['state'] == '연결됨'
  assert stage_map(result)['usb']['detail'] == 'USB 인터페이스 DOWN'


@pytest.mark.parametrize('flags,carrier,operstate,detail', [
  ('0x1002', '', 'down', 'USB 인터페이스 DOWN'),
  ('0x11003', '1', 'up', '물리 연결됨 · Jetson 응답 대기'),
])
@pytest.mark.asyncio
@pytest.mark.filterwarnings('ignore::aiohttp.web_exceptions.NotAppKeyWarning')
async def test_status_api_exposes_offroad_link_state(tmp_path, monkeypatch, jetson, flags, carrier, operstate, detail):
  configure_paths(tmp_path, monkeypatch, jetson, carrier=carrier, flags=flags, operstate=operstate)
  monkeypatch.setattr(jetson, 'snapshot', lambda state: base_result())
  app = web.Application()
  app['params'] = FakeParams({'IsOffroad': True})
  jetson.register(app)
  # This test exercises HTTP status, without claiming a real UDP receiver.
  app.on_startup.clear()
  async with TestClient(TestServer(app)) as client:
    response = await client.get('/api/jetson/status')
    assert response.status == 200
    result = await response.json()

  assert result['ok'] is True
  assert result['local_jetlink']['waiting_modeld'] is True
  assert result['status']['usb_admin_state'] == ('down' if flags == '0x1002' else 'up')
  assert result['status']['usb_operstate'] == operstate
  assert stage_map(result)['usb']['detail'] == detail
