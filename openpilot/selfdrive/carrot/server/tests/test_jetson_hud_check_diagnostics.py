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


def test_check_deduplicates_release_queries_but_refreshes_live_safety(monkeypatch):
  """Many browser tabs share discovery, never a cached safety authorization."""
  monkeypatch.setattr(jetson, 'check_request', lambda _: None)
  monkeypatch.setattr(jetson, 'target', lambda: '')
  checks = {'github': 0, 'safety': 0}
  def release():
    checks['github'] += 1
    return {'commit': 'c'*40, 'size': 250, 'release_url': 'https://github.com'}
  async def safety(_):
    checks['safety'] += 1
    return True, '실시간 차량 안전 상태'
  monkeypatch.setattr(jetson.hud_updater, 'release_info', release)
  monkeypatch.setattr(jetson, 'hud_install_ready', safety)
  monkeypatch.setattr(jetson, '_hud_update_status', lambda: {'stage': 'idle'})
  app = {'jetson_status_state': {}}
  req = NS(app=app)
  async def check():
    a = await jetson.api_hud_update_check(req)
    b = await jetson.api_hud_update_check(req)
    assert json.loads(a.text)['release_ready'] is True
    assert json.loads(b.text)['release_ready'] is True
    assert checks == {'github': 1, 'safety': 2}
    # Explicit expiry forces a fresh lookup; actual installation never uses
    # the cache in either case.
    app['jetson_status_state']['hud_release_lookup_cache']['expires_at'] = 0
    await jetson.api_hud_update_check(req)
    assert checks == {'github': 2, 'safety': 3}
  asyncio.run(check())


def test_unpublished_release_cache_does_not_fabricate_installability(monkeypatch):
  monkeypatch.setattr(jetson, 'check_request', lambda _: None)
  monkeypatch.setattr(jetson, 'target', lambda: '')
  calls = []
  def unpublished():
    calls.append(1)
    raise jetson.hud_updater.PackageNotPublished('f'*40)
  monkeypatch.setattr(jetson.hud_updater, 'release_info', unpublished)
  monkeypatch.setattr(jetson, 'hud_install_ready', lambda app: asyncio.sleep(0, result=(True, 'Offroad')))
  monkeypatch.setattr(jetson, '_hud_update_status', lambda: {'stage': 'idle'})
  req = NS(app={'jetson_status_state': {}})
  async def check():
    a = json.loads((await jetson.api_hud_update_check(req)).text)
    b = json.loads((await jetson.api_hud_update_check(req)).text)
    assert a['release_status'] == b['release_status'] == 'not_published'
    assert a['release_ready'] is b['release_ready'] is False
    assert a['available'] is b['available'] is None
    assert len(calls) == 1
  asyncio.run(check())


def test_duplicate_install_during_ssh_check_and_missing_worker_both_block(monkeypatch):
  import pytest
  from unittest.mock import AsyncMock
  record = {'stage': 'checking_jetson_ssh', 'pid': 42}
  monkeypatch.setattr(jetson, 'read_record', lambda _: record)
  monkeypatch.setattr(jetson, 'target', lambda: 'jetlink@jetson')
  monkeypatch.setattr(jetson, 'check_request', lambda _: None)
  monkeypatch.setattr(jetson, 'hud_install_ready', AsyncMock(return_value=(True, 'Offroad')))
  monkeypatch.setattr(jetson.os, 'kill', lambda *_: None)
  request = NS(app={'jetson_status_state': {'action_lock': asyncio.Lock()}},
               content=NS(read=AsyncMock(return_value=json.dumps({'commit': 'a' * 40, 'confirm': True}).encode())))
  for expected in ('checking_jetson_ssh', 'unknown'):
    assert jetson._hud_update_status()['stage'] == expected
    with pytest.raises(jetson.web.HTTPConflict) as blocked:
      asyncio.run(jetson.api_hud_update_start(request))
    assert '다른 HUD 업데이트' in blocked.value.text
    record['pid'] = None
