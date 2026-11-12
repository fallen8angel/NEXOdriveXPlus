"""Port 7000 unified diagnostic endpoint; no real Jetson or network is contacted."""
import asyncio
from io import BytesIO
import json
from types import SimpleNamespace
from zipfile import ZipFile

from aiohttp import web
import pytest

from openpilot.selfdrive.carrot.server.features import jetson as api


@pytest.fixture
def request_for_bundle(monkeypatch):
  app = {'jetson_status_state': {'csrf': 'unit-test-token', 'transport': None,
                                 'channels': {}, 'action_lock': asyncio.Lock(),
                                 'link_history': SimpleNamespace(export=lambda: {'events': [], 'count': 0})}}
  monkeypatch.setattr(api, 'snapshot', lambda state: {'connected': False, 'stages': []})
  monkeypatch.setattr(api, '_local_jetlink_state', lambda app, state: {'enabled': False})
  monkeypatch.setattr(api, '_apply_local_jetlink_context', lambda state, local: None)
  monkeypatch.setattr(api, '_safe_link_diagnostics', lambda local, state: {'summary': 'available'})
  async def report(_app):
    return {'ok': True, 'assessment': {'code': 'WAITING_FOR_JETSON'}}
  monkeypatch.setattr(api, '_local_diagnostic_report', report)
  monkeypatch.setattr(api, 'report_text', lambda value: '진단 확인')
  return SimpleNamespace(app=app, headers={'X-Jetson-Token': 'unit-test-token'},
                         content_type='application/json', host='127.0.0.1:7000')


@pytest.mark.asyncio
async def test_bundle_keeps_comma_diagnostics_when_ssh_not_configured(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: '')
  response = await api.api_jetson_bundle(request_for_bundle)
  assert response.status == 200
  assert response.content_type == 'application/zip'
  with ZipFile(BytesIO(response.body)) as archive:
    names = archive.namelist()
    assert '01_comma_status.json' in names
    assert '02_usb_diagnostics.json' in names
    assert '03_connection_history.txt' in names
    manifest = json.loads(archive.read('00_manifest.json'))
    assert 'remote_ssh' in manifest['unavailable_or_failed']
    assert '04_comma_ai_performance.json' in names
    assert '05_ai_hud_performance_summary.txt' in names


@pytest.mark.asyncio
async def test_bundle_collects_only_read_only_remote_commands(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: 'jetson-host')
  actions = []
  async def ssh(target, action):
    actions.append(action)
    if action == 'logs':
      return {'ok': True, 'logs': 'no secrets'}
    return {'ok': True, 'action': action}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  response = await api.api_jetson_bundle(request_for_bundle)
  assert response.status == 200
  assert set(actions) == {'status', 'video_test', 'model', 'logs', 'settings', 'hud_trace', 'performance', 'performance_history'}
  with ZipFile(BytesIO(response.body)) as archive:
    assert 'remote/09_hud_trace.json' in archive.namelist()
    assert 'remote/05_video_test.json' in archive.namelist()
    assert 'remote/07_logs.txt' in archive.namelist()
    assert 'remote/10_ai_hud_performance.json' in archive.namelist()
    assert 'remote/11_ai_hud_history.json' in archive.namelist()
    assert '04_comma_ai_performance.json' in archive.namelist()
    assert '05_ai_hud_performance_summary.json' in archive.namelist()


@pytest.mark.asyncio
async def test_bundle_refuses_invalid_token_without_collecting(request_for_bundle):
  request_for_bundle.headers['X-Jetson-Token'] = 'incorrect'
  with pytest.raises(web.HTTPForbidden):
    await api.api_jetson_bundle(request_for_bundle)

@pytest.mark.asyncio
async def test_bundle_retries_only_timed_out_remote_logs(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: 'jetson-host')
  attempts = []
  async def ssh(target, action):
    attempts.append(action)
    if action == 'logs' and attempts.count('logs') == 1:
      return {'ok': False, 'error': 'TimeoutError: remote query timed out'}
    if action == 'logs':
      return {'ok': True, 'logs': 'bounded journal'}
    return {'ok': True, 'action': action}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    assert archive.read('remote/07_logs.txt') == b'bounded journal'
    manifest = json.loads(archive.read('00_manifest.json'))
    assert 'remote/07_logs.json' not in manifest['unavailable_or_failed']
  assert attempts.count('logs') == 2
