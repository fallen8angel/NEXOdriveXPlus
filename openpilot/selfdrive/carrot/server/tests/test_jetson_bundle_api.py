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
  monkeypatch.setattr(api.lte_share, 'status', lambda: {'state': 'not_configured'})
  monkeypatch.setattr(api, 'collect_ai_performance', lambda: {'inference_state': 'disabled'})
  return SimpleNamespace(app=app, headers={'X-Jetson-Token': 'unit-test-token'},
                         content_type='application/json', host='127.0.0.1:7000')


@pytest.mark.asyncio
@pytest.mark.parametrize('action,confirmed,offroad,enabled,active,expected', [
  ('status', False, False, True, True, 200), ('start', False, True, False, False, 400),
  ('start', True, False, False, False, 409), ('start', True, True, True, False, 409),
  ('start', True, True, False, True, 409), ('start', True, True, False, False, 200)])
async def test_engine_experiment_gate(request_for_bundle, monkeypatch, tmp_path, action, confirmed, offroad, enabled, active, expected):
  request = request_for_bundle
  marker = tmp_path / 'enabled'
  if enabled:
    marker.touch()
  monkeypatch.setattr(api, 'JETLINK_ENABLED', marker)
  monkeypatch.setattr(api, 'target', lambda: 'test-peer')
  monkeypatch.setattr(api.lte_share, 'offroad', lambda: offroad)
  monkeypatch.setattr(api, 'snapshot', lambda _: {'native_link': {'model_active': active}})
  calls = []
  async def ssh(*args):
    calls.append(args)
    return {'ok': True}
  async def read(_):
    return json.dumps(dict(action=action, confirm=confirmed, comma_user='comma')).encode()
  monkeypatch.setattr(api, 'ssh_action', ssh)
  request.content = SimpleNamespace(read=read)
  try:
    response = await api.api_engine_experiment(request)
  except web.HTTPBadRequest as error:
    response = error
  assert response.status == expected
  assert calls == ([] if expected != 200 else [('test-peer', 'engine_experiments')] if action == 'status' else
                   [('test-peer', 'start_engine_experiment', True, 'comma', None)])


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
  assert set(actions) == {'status', 'video_test', 'model', 'logs', 'settings', 'hud_trace', 'performance',
                          'performance_history', 'inference_readiness', 'engine_experiments', 'ssh_diagnostics', 'engine_candidates'}
  with ZipFile(BytesIO(response.body)) as archive:
    assert 'remote/09_hud_trace.json' in archive.namelist()
    assert 'remote/05_video_test.json' in archive.namelist()
    assert 'remote/07_logs.txt' in archive.namelist()
    assert 'remote/10_ai_hud_performance.json' in archive.namelist()
    assert 'remote/11_ai_hud_history.json' in archive.namelist()
    assert 'remote/12_ai_inference_readiness.json' in archive.namelist()
    assert 'remote/15_engine_candidate_validation.json' in archive.namelist()
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
@pytest.mark.parametrize('installed, expected', [(None, None), ('a' * 40, False), ('b' * 40, True), ('malformed', None)])
async def test_bundle_exports_completed_update_without_fabricated_failure(request_for_bundle, monkeypatch, installed, expected):
  monkeypatch.setattr(api, 'target', lambda: 'jetson' if installed else '')
  async def ssh(target, action):
    return {'ok': True, 'status': {'summary': {'jetson_version': installed}}} if action == 'status' else {'ok': True}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  monkeypatch.setattr(api, '_hud_update_status', lambda: {
    'stage': 'completed', 'commit': 'b' * 40, 'updated': 1790000000})
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    info = json.loads(archive.read('06_jetson_update_diagnostics.json'))
    assert info['has_last_attempt'] is True
    assert info['stage'] == 'completed'
    assert info['failure_code'] is None
    assert info['installed_matches_target'] is expected
    assert info['jetson_installed_commit'] == (installed if expected is not None else None)


@pytest.mark.asyncio
async def test_readiness_reports_hud_only_without_ever_installing(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: 'jetson-host')
  monkeypatch.setattr(api, '_local_jetlink_state', lambda *args: {
    'enabled': False, 'onroad': False, 'offroad': True})
  async def ssh(target, action):
    if action == 'inference_readiness':
      return {'ok': True, 'inference_readiness': {
        'install_state': 'display_only_installed', 'display_only_installed': True,
        'engine_file_present': False, 'model_file_ready': True, 'installed_sha256': 'f' * 64,
        'engine_build_errors': [{'file': 'f' * 16 + '.trt10.3.0.Orin-sm87.build.json',
                                 'failure_diagnosis': {'category': 'conv_gelu_tactic_unavailable'}}],
        'blockers': ['HUD_ONLY_RUNTIME', 'CURRENT_TRT_ENGINE_NOT_BUILT']}}
    return {'ok': True, 'action': action}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    report = json.loads(archive.read('07_ai_inference_readiness.json'))
    assert report['can_activate_now'] is False
    assert report['auto_install_or_build'] is False
    assert report['comma_offroad'] is True
    assert 'HUD_ONLY_RUNTIME' in report['blockers']
    assert '--experiment' in report['recommendation'] and 'Offroad=1' in report['recommendation']


@pytest.mark.asyncio
async def test_reverse_ssh_probe_is_read_only_and_preserves_structured_failure(request_for_bundle, monkeypatch):
  request = request_for_bundle
  monkeypatch.setattr(api, 'target', lambda: 'jetlink@192.168.100.250')
  monkeypatch.setattr(api.lte_share, 'offroad', lambda: (_ for _ in ()).throw(AssertionError('read-only probe needs no mutation gate')))
  calls = []
  async def ssh(*args, **kwargs):
    calls.append((args, kwargs))
    return {'ok': True, 'comma_to_jetson_verified': True,
            'ssh_preflight': {'ok': False, 'code': 'SSH_HOST_KEY_CHANGED', 'settings_changed': False}}
  async def read(_):
    return json.dumps({'action': 'ssh', 'comma_user': 'comma', 'identity_file': '/home/jetlink/.ssh/approved'}).encode()
  monkeypatch.setattr(api, 'ssh_action', ssh)
  request.content = SimpleNamespace(read=read)
  response = await api.api_engine_experiment(request)
  value = json.loads(response.body)
  assert response.status == 200 and value['ssh_preflight']['code'] == 'SSH_HOST_KEY_CHANGED'
  assert calls == [(('jetlink@192.168.100.250', 'comma_ssh_preflight', True, 'comma'),
                    {'comma_identity': '/home/jetlink/.ssh/approved'})]
  assert request.app['jetson_status_state']['last_comma_ssh_preflight']['settings_changed'] is False



@pytest.mark.asyncio
async def test_unified_zip_adds_ai_root_cause_without_jetson_ssh(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: '')
  monkeypatch.setattr(api, 'comma_model', lambda: ('f' * 64, {}))
  response = await api.api_jetson_bundle(request_for_bundle)
  assert response.status == 200
  with ZipFile(BytesIO(response.body)) as archive:
    assert '10_ai_root_cause.json' in archive.namelist()
    assert '10_ai_root_cause.txt' in archive.namelist()
    assert '10_comma_ai_model_identity.json' in archive.namelist()
    report = json.loads(archive.read('10_ai_root_cause.json'))
    assert report['stage_code'] == 'remote_unavailable'
    assert report['root_cause_proven'] is False
    assert report['engine_build_performed'] is False
    assert report['model']['comma_jetson_sha_match'] is None
    status = json.loads(archive.read('00_function_status.json'))
    assert status['ai_diagnosis_stage'] == 'remote_unavailable'


@pytest.mark.asyncio
async def test_unified_zip_ai_build_failure_is_attributed_only_to_current_model(request_for_bundle, monkeypatch):
  sha = 'f' * 64
  monkeypatch.setattr(api, 'target', lambda: 'jetson-host')
  monkeypatch.setattr(api, 'comma_model', lambda: (sha, {}))
  actions = []
  async def ssh(_target, action):
    actions.append(action)
    if action == 'inference_readiness':
      return {'ok': True, 'inference_readiness': {
        'installed_sha256': sha, 'engine_file_present': False,
        'synthetic_inference_verified': False,
        'package_model_matches': True, 'ncm_model_matches': True,
        'engine_build_errors': [{'file': sha[:16] + '.trt10.3.0.Orin-sm87.build.json',
          'failure_diagnosis': {'category': 'conv_gelu_tactic_unavailable'}}]}}
    if action == 'model':
      return {'ok': True, 'installed_model': {'onnx_sha256': sha,
        'package_contract': {'sha256': sha}, 'package_runtime': {'tensorrt': '10.3.0'}}}
    if action == 'engine_experiments':
      return {'ok': True, 'experiments': {'available': False, 'reason': 'not_run'}}
    return {'ok': True}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  response = await api.api_jetson_bundle(request_for_bundle)
  assert response.status == 200
  with ZipFile(BytesIO(response.body)) as archive:
    report = json.loads(archive.read('10_ai_root_cause.json'))
    assert report['stage_code'] == 'engine_build_failed'
    assert report['root_cause_category'] == 'conv_gelu_tactic_unavailable'
    assert report['model']['comma_jetson_sha_match'] is True
    assert report['root_cause_proven'] is False
    assert report['engine']['gpu_synthetic_verified'] is False
    assert 'Conv2D + GELU' in archive.read('10_ai_root_cause.txt').decode('utf-8')
  assert 'start_engine_experiment' not in actions
  assert 'comma_ssh_preflight' not in actions



@pytest.mark.asyncio
async def test_unified_zip_contains_offroad_trial_snapshot_without_starting_engine(request_for_bundle, monkeypatch):
  monkeypatch.setattr(api, 'target', lambda: '')
  monkeypatch.setattr(api, '_local_jetlink_state', lambda *args: {
    'offroad': True, 'onroad': False, 'enabled': False})
  monkeypatch.setattr(api.lte_share, 'offroad', lambda: True)
  state = request_for_bundle.app['jetson_status_state']
  state['last_engine_experiment_start'] = {
    'ok': False, 'trial': 'native_fp16', 'error': 'SSH_HOST_KEY_UNKNOWN', 'time': 123}
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    assert '11_offroad_ai_engine_trial.json' in archive.namelist()
    assert '11_offroad_ai_engine_trial.txt' in archive.namelist()
    data = json.loads(archive.read('11_offroad_ai_engine_trial.json'))
    assert data['bundle_collection_read_only'] is True
    assert data['trial_started_by_bundle'] is False
    assert data['safety']['full_offroad_confirmed_at_collection'] is True
    assert data['safety']['eligible_local_gate_at_collection'] is True
    assert data['launch']['accepted'] is False
    assert data['launch']['requested_trial'] == 'native_fp16'
    assert data['experiment']['available'] is False
    assert data['vehicle_inference_verified'] is False
    assert b'SSH_HOST_KEY_UNKNOWN' in archive.read('11_offroad_ai_engine_trial.txt')


@pytest.mark.asyncio
async def test_unified_zip_includes_completed_native_gpu_trial_only_for_matching_sha(request_for_bundle, monkeypatch):
  sha = 'f' * 64
  monkeypatch.setattr(api, 'target', lambda: 'jetson-host')
  monkeypatch.setattr(api, '_local_jetlink_state', lambda *args: {
    'offroad': True, 'onroad': False, 'enabled': False})
  monkeypatch.setattr(api.lte_share, 'offroad', lambda: True)
  monkeypatch.setattr(api, 'comma_model', lambda: (sha, {}))
  actions = []
  async def ssh(_target, action):
    actions.append(action)
    if action == 'engine_experiments':
      return {'ok': True, 'experiments': {
        'available': True, 'state': 'passed', 'model_sha256': sha,
        'native_gpu_verified': True,
        'trials': [{'name': 'native_fp16', 'state': 'passed', 'synthetic_fps': 22.1}]}}
    return {'ok': True}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  response = await api.api_jetson_bundle(request_for_bundle)
  with ZipFile(BytesIO(response.body)) as archive:
    report = json.loads(archive.read('11_offroad_ai_engine_trial.json'))
    assert report['experiment']['matches_current_comma_model'] is True
    assert report['experiment']['native_gpu_verified_for_matching_model'] is True
    assert report['vehicle_inference_verified'] is False
    assert 'start_engine_experiment' not in actions
    assert archive.read('11_offroad_ai_engine_trial.txt').decode().find('native_fp16') >= 0
