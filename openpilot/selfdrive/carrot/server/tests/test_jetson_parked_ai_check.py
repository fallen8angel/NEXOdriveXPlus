"""Parked-onroad AI readiness reads: no TensorRT build, vehicle writes or service changes."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from openpilot.selfdrive.carrot.server.features import jetson as api


def request(action='parked_check', token='test-token'):
  async def read(_limit):
    return json.dumps({'action': action}).encode('utf-8')
  return SimpleNamespace(
    app={'jetson_status_state': {'csrf': 'test-token', 'action_lock': asyncio.Lock()}},
    headers={'X-Jetson-Token': token}, content_type='application/json',
    content=SimpleNamespace(read=read),
  )


def configure(monkeypatch, *, allowed=True, stages=('idle',), remote_ok=True, same_sha=True):
  calls = []
  park_calls = []
  statuses = iter(stages)
  monkeypatch.setattr(api, 'target', lambda: 'jetlink@192.168.100.250')
  monkeypatch.setattr(api, '_hud_update_status', lambda: {'stage': next(statuses, 'idle')})
  def parked():
    park_calls.append(True)
    return allowed, '실시간 P단·완전 정지·주행 제어 비활성 확인' if allowed else 'P단 확인 불가'
  monkeypatch.setattr(api.hud_updater, 'installation_ready', parked)
  async def ssh(target, action, *args, **kwargs):
    calls.append((target, action))
    if not remote_ok:
      return {'ok': False, 'error': 'SSH unavailable'}
    return {'ok': True, 'inference_readiness': {
      'installed_sha256': 'a' * 64 if same_sha else 'b' * 64,
      'model_file_ready': True, 'engine_file_present': False,
      'blockers': ['CURRENT_TRT_ENGINE_NOT_BUILT']}}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  monkeypatch.setattr(api, 'comma_model', lambda: ('a' * 64, {}))
  return calls, park_calls


@pytest.mark.asyncio
async def test_parked_readonly_works_on_verified_p_without_reverse_ssh(monkeypatch):
  calls, park_calls = configure(monkeypatch)
  resp = await api.api_engine_experiment(request())
  data = json.loads(resp.body)
  assert resp.status == 200 and data['ok']
  assert data['mode'] == 'parked_read_only'
  assert data['model_sha_match'] is True
  assert data['engine_build_performed'] is False
  assert data['driving_activation_performed'] is False
  assert data['vehicle_inference_verified'] is False
  assert calls == [('jetlink@192.168.100.250', 'inference_readiness')]
  assert len(park_calls) == 2


@pytest.mark.asyncio
async def test_unverified_p_does_not_contact_jetson(monkeypatch):
  calls, _ = configure(monkeypatch, allowed=False)
  resp = await api.api_engine_experiment(request())
  assert resp.status == 409
  assert json.loads(resp.body)['code'] == 'PARK_NOT_VERIFIED'
  assert calls == []


@pytest.mark.asyncio
async def test_concurrent_hud_update_prevents_parked_check(monkeypatch):
  calls, _ = configure(monkeypatch, stages=('installing',))
  resp = await api.api_engine_experiment(request())
  assert resp.status == 409
  assert json.loads(resp.body)['code'] == 'HUD_UPDATE_BUSY'
  assert not calls


@pytest.mark.asyncio
async def test_ssh_failure_does_not_claim_ai_ready(monkeypatch):
  calls, _ = configure(monkeypatch, remote_ok=False)
  resp = await api.api_engine_experiment(request())
  assert resp.status == 502
  assert json.loads(resp.body)['code'] == 'READINESS_UNAVAILABLE'
  assert calls == [('jetlink@192.168.100.250', 'inference_readiness')]


@pytest.mark.asyncio
async def test_park_loss_during_read_does_not_report_success(monkeypatch):
  calls, _ = configure(monkeypatch)
  checks = iter([(True, 'P confirmed'), (False, 'Drive selected')])
  monkeypatch.setattr(api.hud_updater, 'installation_ready', lambda: next(checks))
  resp = await api.api_engine_experiment(request())
  assert resp.status == 409
  assert json.loads(resp.body)['code'] == 'PARK_STATE_CHANGED'
  assert calls == [('jetlink@192.168.100.250', 'inference_readiness')]


@pytest.mark.asyncio
async def test_sha_mismatch_explicit_and_no_build(monkeypatch):
  configure(monkeypatch, same_sha=False)
  resp = await api.api_engine_experiment(request())
  data = json.loads(resp.body)
  assert resp.status == 200
  assert data['model_sha_match'] is False
  assert data['engine_build_performed'] is False


@pytest.mark.asyncio
async def test_csrf_token_required(monkeypatch):
  calls, _ = configure(monkeypatch)
  with pytest.raises(web.HTTPForbidden):
    await api.api_engine_experiment(request(token='invalid'))
  assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['n16_precision_pair', 'n16_boundary_trace'])
async def test_n16_short_trial_start_needs_live_offroad_and_inactive_ai(monkeypatch, tmp_path, selected):
  req = request()
  async def read(_):
    return json.dumps({'action': 'start', 'confirm': True, 'trial': selected, 'comma_user': 'comma'}).encode()
  req.content.read = read
  monkeypatch.setattr(api, 'target', lambda: 'jetlink@jetson')
  monkeypatch.setattr(api, 'snapshot', lambda _: {})
  monkeypatch.setattr(api, '_hud_update_status', lambda: {'stage': 'idle'})
  enabled = tmp_path / 'enabled'
  monkeypatch.setattr(api, 'JETLINK_ENABLED', enabled)
  calls = []
  async def ssh(*args):
    calls.append(args)
    return {'ok': True, 'experiment': {'state': 'accepted'}}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  monkeypatch.setattr(api.lte_share, 'offroad', lambda: False)
  assert (await api.api_engine_experiment(req)).status == 409
  monkeypatch.setattr(api.lte_share, 'offroad', lambda: True)
  enabled.touch()
  assert (await api.api_engine_experiment(req)).status == 409 and not calls
  enabled.unlink()
  assert (await api.api_engine_experiment(req)).status == 200
  assert calls == [('jetlink@jetson', 'start_engine_experiment', True, 'comma', selected)]


@pytest.mark.asyncio
async def test_short_status_read_never_launches_trial(monkeypatch):
  req = request()
  async def read(_):
    return b'{"action":"status","trial":"n16_precision_pair"}'
  req.content.read = read
  monkeypatch.setattr(api, 'target', lambda: 'jetlink@jetson')
  async def ssh(_, action):
    assert action == 'n16_partition_parity_status'
    return {'ok': True, 'offline_carrot_precision_pair': {'state': 'running', 'phase': 'gpu_plain'}}
  monkeypatch.setattr(api, 'ssh_action', ssh)
  data = json.loads((await api.api_engine_experiment(req)).body)
  assert data['experiment'] == {'state': 'running', 'phase': 'gpu_plain'}
