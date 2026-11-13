"""HUD update check reports installation blockers separately (read-only)."""
import asyncio
import json
from types import SimpleNamespace as NS

from openpilot.selfdrive.carrot.server.features import jetson


def test_check_missing_release_still_shows_remote_and_park_safety(monkeypatch):
  monkeypatch.setattr(jetson, 'check_request', lambda request: None)
  monkeypatch.setattr(jetson, 'target', lambda: 'jetlink@192.168.100.250')
  def missing_release():
    raise jetson.hud_updater.PackageNotPublished('b'*40)
  monkeypatch.setattr(jetson.hud_updater, 'release_info', missing_release)
  async def remote_check(*args):
    return {'ok': True, 'status': {'summary': {'jetson_version': 'old-commit'}}}
  async def safety_check(app):
    return False, '차량 데이터 미수신'
  monkeypatch.setattr(jetson, 'ssh_action', remote_check)
  monkeypatch.setattr(jetson, 'hud_install_ready', safety_check)
  monkeypatch.setattr(jetson, '_hud_update_status', lambda: {'stage': 'completed', 'commit': 'old-commit'})
  response = asyncio.run(jetson.api_hud_update_check(NS(app={})))
  value = json.loads(response.text)
  assert response.status == 200 and value['ok']
  assert value['release_ready'] is False and value['release'] is None
  assert value['release_status'] == 'not_published' and value['latest_commit'] == 'b'*40
  assert value['available'] is None and value['installed'] == 'old-commit'
  assert '아직 생성되지 않았습니다' in value['release_error']
  assert value['safety_reason'] == '차량 데이터 미수신'
  assert value['ssh_configured'] is True
  assert value['can_install_safely'] is False


def test_check_verified_release_preserves_regular_result(monkeypatch):
  monkeypatch.setattr(jetson, 'check_request', lambda request: None)
  monkeypatch.setattr(jetson, 'target', lambda: '')
  monkeypatch.setattr(jetson.hud_updater, 'release_info',
                      lambda: {'commit': 'a'*40, 'size': 123, 'release_url': 'https://github.com'})
  async def safety_check(app):
    return True, '정상 Offroad 상태'
  monkeypatch.setattr(jetson, 'hud_install_ready', safety_check)
  monkeypatch.setattr(jetson, '_hud_update_status', lambda: {'stage': 'idle'})
  response = asyncio.run(jetson.api_hud_update_check(NS(app={})))
  value = json.loads(response.text)
  assert response.status == 200 and value['ok'] and value['release_ready'] is True
  assert value['release_status'] == 'ready' and value['latest_commit'] == 'a'*40
  assert value['release']['commit'] == 'a'*40 and value['release_error'] == ''
  assert value['available'] is None and value['ssh_configured'] is False
  assert value['can_install_safely'] is True
