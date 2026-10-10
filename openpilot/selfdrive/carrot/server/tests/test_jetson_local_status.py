from pathlib import Path

from openpilot.selfdrive.carrot.server.features import jetson


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


def configure_paths(tmp_path, monkeypatch, carrier='1'):
  marker = tmp_path / 'nexo_jetlink_enabled'
  marker.touch()
  carrier_path = tmp_path / 'carrier'
  carrier_path.write_text(carrier, encoding='utf-8')
  monkeypatch.setattr(jetson, 'JETLINK_ENABLED', marker)
  monkeypatch.setattr(jetson, 'USB0_CARRIER', carrier_path)


def test_enabled_offroad_reports_modeld_wait_instead_of_disconnected(tmp_path, monkeypatch):
  configure_paths(tmp_path, monkeypatch)
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
  assert stages['jetlink']['state'] == 'WAITING'
  assert stages['camera']['state'] == 'WAITING'
  assert stages['inference']['state'] == 'WAITING'
  assert stages['hud']['state'] == 'WAITING'


def test_enabled_display_usb_conflict_is_explicit(tmp_path, monkeypatch):
  configure_paths(tmp_path, monkeypatch)
  app = {'params': FakeParams({'IsOnroad': False, 'IsOffroad': True, 'NexoJetsonUsb': True})}
  result = base_result()

  local = jetson._local_jetlink_state(app, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['conflict'] is True
  assert result['state'] == '오류'
  assert 'NexoJetsonUsb' in result['status']['last_error']
  assert stage_map(result)['jetlink']['state'] == 'ERROR'


def test_disabled_mode_keeps_disconnected_snapshot_unchanged(tmp_path, monkeypatch):
  marker = tmp_path / 'missing_marker'
  carrier_path = tmp_path / 'carrier'
  carrier_path.write_text('1', encoding='utf-8')
  monkeypatch.setattr(jetson, 'JETLINK_ENABLED', marker)
  monkeypatch.setattr(jetson, 'USB0_CARRIER', carrier_path)
  app = {'params': FakeParams({'IsOnroad': False, 'IsOffroad': True, 'NexoJetsonUsb': False})}
  result = base_result()

  local = jetson._local_jetlink_state(app, result)
  jetson._apply_local_jetlink_context(result, local)

  assert local['enabled'] is False
  assert result['state'] == '미연결'
  assert stage_map(result)['jetlink']['state'] == 'DISCONNECTED'
