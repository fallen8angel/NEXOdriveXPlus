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
  monkeypatch.setattr(api, '_hud_update_status', lambda: {'stage': 'idle'})
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
    assert '04_comma_ai_history.json' in names
    assert '05_ai_hud_performance_summary.txt' in names
    assert '06_jetson_update_diagnostics.json' in names
    assert '06_jetson_update_diagnostics.txt' in names
    assert '07_ai_inference_readiness.json' in names
    assert json.loads(archive.read('07_ai_inference_readiness.json'))['can_activate_now'] is False
    update = json.loads(archive.read('06_jetson_update_diagnostics.json'))
    assert update['has_last_attempt'] is False


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
  assert set(actions) == {'status', 'video_test', 'model', 'logs', 'settings', 'hud_trace', 'performance', 'performance_history', 'inference_readiness'}
  with ZipFile(BytesIO(response.body)) as archive:
    assert 'remote/09_hud_trace.json' in archive.namelist()
    assert 'remote/05_video_test.json' in archive.namelist()
    assert 'remote/07_logs.txt' in archive.namelist()
    assert 'remote/10_ai_hud_performance.json' in archive.namelist()
    assert 'remote/11_ai_hud_history.json' in archive.namelist()
    assert 'remote/12_ai_inference_readiness.json' in archive.namelist()
    assert '07_ai_inference_readiness.txt' in archive.namelist()
    assert '04_comma_ai_performance.json' in archive.namelist()
    assert '05_ai_hud_performance_summary.json' in archive.namelist()
    assert '06_jetson_update_diagnostics.json' in archive.namelist()


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


@pytest.mark.asyncio
async def test_bundle_exports_latest_update_failure_without_raw_secrets(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: '')  # works while Jetson is unreachable
  monkeypatch.setattr(api, '_hud_update_status', lambda: {
    'stage': 'failed', 'failed_stage': 'checking_jetson_ssh',
    'failure_code': 'SSH_AUTH', 'retry_safe': True,
    'commit': 'a' * 40, 'updated': 1790000000,
    'detail': 'Permission denied: password=my-secret jetlink@192.168.100.250',
    'ssh_target': 'jetlink@192.168.100.250',
    'advice': 'unsafe arbitrary text with token=private'})
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    names = archive.namelist()
    assert '06_jetson_update_diagnostics.json' in names
    info = json.loads(archive.read('06_jetson_update_diagnostics.json'))
    assert info['failure_code'] == 'SSH_AUTH'
    assert info['failed_stage'] == 'checking_jetson_ssh'
    assert info['manual_retry_possible_after_checks'] is True
    for filename in ('06_jetson_update_diagnostics.json', '06_jetson_update_diagnostics.txt'):
      content = archive.read(filename).decode()
      assert 'my-secret' not in content
      assert '192.168.100.250' not in content
      assert 'token=private' not in content


@pytest.mark.asyncio
async def test_bundle_exports_completed_update_without_fabricated_failure(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: '')
  monkeypatch.setattr(api, '_hud_update_status', lambda: {
    'stage': 'completed', 'commit': 'b' * 40, 'updated': 1790000000})
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    info = json.loads(archive.read('06_jetson_update_diagnostics.json'))
    assert info['has_last_attempt'] is True
    assert info['stage'] == 'completed'
    assert info['failure_code'] is None


@pytest.mark.asyncio
async def test_readiness_reports_hud_only_without_ever_installing(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: 'jetson-host')
  monkeypatch.setattr(api, '_local_jetlink_state', lambda *args: {
    'enabled': False, 'onroad': False, 'offroad': True})
  async def ssh(target, action):
    if action == 'inference_readiness':
      return {'ok': True, 'inference_readiness': {
        'install_state': 'display_only_installed', 'display_only_installed': True,
        'engine_file_present': False, 'blockers': ['HUD_ONLY_RUNTIME', 'CURRENT_TRT_ENGINE_NOT_BUILT']}}
    return {'ok': True, 'action': action}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    report = json.loads(archive.read('07_ai_inference_readiness.json'))
    assert report['can_activate_now'] is False
    assert report['auto_install_or_build'] is False
    assert report['comma_offroad'] is True
    assert 'HUD_ONLY_RUNTIME' in report['blockers']
