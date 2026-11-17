"""Management evidence leases and passive sampling; no installed GPU required."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from openpilot.selfdrive.carrot.server.services.jetson_performance import management_overview


def history(age=1):
  return {'ok': True, 'history': {'available': True, 'fresh': True, 'age_s': age,
    'samples': [{'gpu_load_pct': 38, 'gpu_clock_mhz': 918}]}}


def test_hud_gap_report_keeps_stage_latency_separate_from_causation():
  from openpilot.selfdrive.carrot.server.services.jetson_performance import format_text
  value = {'inference': {'inference_state': 'disabled', 'external_inference_proven': False},
           'jetson_performance_collected': True,
           'evidence': {'hud_pipeline': {
             'panel_gap_count': 1, 'panel_gap_max_s': 0.1905, 'panel_gap_threshold_s': 0.15,
             'render_ms': 6.85, 'encoder_wait_ms': 32.13, 'panel_send_ms': 1.56}}}
  text = format_text(value)
  assert '190.5ms' in text and '150.0ms' in text
  assert '렌더링: 6.85ms' in text and '인코더 대기: 32.13ms' in text
  assert '실제 화면 꺼짐 원인 미확정' in text


def test_engine_ready_and_gpu_busy_do_not_prove_inference():
  value = management_overview({'model_ready': True, 'external_inference_proven': False,
    'inference_fps': 99, 'timing': {'jetson_execution_us': 2930}}, history(), receipt_age_s=1)
  assert value['gpu_load_pct'] == 38 and value['model_ready'] is True
  assert value['external_inference_proven'] is False and value['inference_fps'] is None
  assert value['last_execution_ms'] is None and not value['vehicle_verified']


@pytest.mark.parametrize('age,receipt', [(19, 2), (1, 12), (-1, 0), (float('nan'), 0), (1, None)])
def test_stale_invalid_or_unreceived_gpu_sample_is_not_current(age, receipt):
  value = management_overview({}, history(age), receipt_age_s=receipt)
  assert value['gpu_load_pct'] is None and not value['gpu_sample_fresh']


@pytest.mark.parametrize('load', [True, float('inf'), float('nan'), -1, 101, 10**1000])
def test_invalid_gpu_measurements_are_not_displayed(load):
  remote = history()
  remote['history']['samples'][-1]['gpu_load_pct'] = load
  assert management_overview({}, remote, receipt_age_s=1)['gpu_load_pct'] is None


def test_live_statistics_have_separate_units_and_remaining_proof_lease():
  value = management_overview({'external_inference_proven': True, 'inference_fps': 19.8,
    'inference_valid_for_s': 1.25,
    'timing': {'jetson_execution_us': 2930, 'modeld_roundtrip_ms': 14},
    'inference_window': {'count': 20, 'roundtrip_p95_ms': 17, 'roundtrip_max_ms': 23}},
    history(), receipt_age_s=1)
  assert value['inference_valid_for_s'] == 1.25
  assert value['last_execution_ms'] == 2.93 and value['last_roundtrip_ms'] == 14
  assert value['roundtrip_p95_ms'] == 17 and value['roundtrip_max_ms'] == 23
  assert value['inference_fps'] == 19.8 and 'mean_ms' not in value


@pytest.fixture
def jetson(monkeypatch):
  # Import optional route in isolation; never stub the safety/msgq interfaces.
  package = ModuleType('ai_performance_route_tests')
  package.__path__ = [str(Path(__file__).resolve().parents[1] / 'features')]
  monkeypatch.setitem(sys.modules, package.__name__, package)
  spec = importlib.util.spec_from_file_location(f'{package.__name__}.jetson', Path(package.__path__[0]) / 'jetson.py')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.mark.asyncio
async def test_slow_ssh_does_not_block_http_and_has_single_inflight_poll(jetson, monkeypatch):
  state = {'action_lock': asyncio.Lock()}
  request = SimpleNamespace(app={'jetson_status_state': state})
  release = asyncio.Event()
  calls = []
  async def ssh(target, action):
    calls.append((target, action))
    await release.wait()
    return history()
  monkeypatch.setattr(jetson, 'ssh_action', ssh)
  monkeypatch.setattr(jetson, 'target', lambda: 'jetlink@192.168.100.250')
  monkeypatch.setattr(jetson, 'collect_ai_performance', lambda: {'inference_state': 'disabled'})
  try:
    for _ in range(3):
      response = await asyncio.wait_for(jetson.api_jetson_ai_performance(request), 2)
      assert json.loads(response.text)['gpu_load_pct'] is None
    assert calls == [('jetlink@192.168.100.250', 'performance_history')]
    release.set()
    await state['ai_history_task']
    response = await jetson.api_jetson_ai_performance(request)
    assert json.loads(response.text)['gpu_load_pct'] == 38
    assert len(calls) == 1
    # Cached data from a different configured SSH host is not presented.
    await state['action_lock'].acquire()
    monkeypatch.setattr(jetson, 'target', lambda: 'jetlink@other')
    response = await jetson.api_jetson_ai_performance(request)
    assert json.loads(response.text)['gpu_load_pct'] is None
    assert len(calls) == 1
    state['action_lock'].release()
  finally:
    await jetson._stop_ai_performance(request.app)


@pytest.mark.asyncio
async def test_disconnect_clears_cached_gpu_and_cleanup_cancels_poll(jetson, monkeypatch):
  state = {'action_lock': asyncio.Lock()}
  request = SimpleNamespace(app={'jetson_status_state': state})
  async def ssh(*_args):
    return {'ok': False, 'error': 'Connection refused'}
  monkeypatch.setattr(jetson, 'ssh_action', ssh)
  monkeypatch.setattr(jetson, 'target', lambda: 'jetlink@192.168.100.250')
  monkeypatch.setattr(jetson, 'collect_ai_performance', dict)
  await jetson.api_jetson_ai_performance(request)
  await state['ai_history_task']
  response = await jetson.api_jetson_ai_performance(request)
  assert json.loads(response.text)['gpu_error'] == 'Connection refused'
  state['ai_history_task'] = asyncio.create_task(asyncio.Event().wait())
  await jetson._stop_ai_performance(request.app)
  assert state['ai_history_task'].cancelled()


def test_page_javascript_compiles_and_expires_on_without_a_successful_refresh(tmp_path):
  path = Path(__file__).resolve().parents[1] / 'features/jetson_pages.py'
  spec = importlib.util.spec_from_file_location('ai_performance_page', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  script = module.PAGE.split('<script>', 1)[1].split('</script>', 1)[0]
  full = tmp_path / 'page.js'
  full.write_text(script)
  subprocess.run(['node', '--check', str(full)], check=True, capture_output=True)
  section = script.split('let aiPerformanceBusy=', 1)[1].split('refreshAiPerformance();setInterval', 1)[0]
  harness = """
const assert=require('node:assert/strict');let now=100,periodic;
const elements={aiLiveState:{},aiLiveMetrics:{}};const $=id=>elements[id];
const performance={now:()=>now};const document={visibilityState:'visible',addEventListener:()=>{}};
const setInterval=fn=>{periodic=fn};
let sample={external_inference_proven:true,inference_valid_for_s:0.25,inference_fps:20,gpu_load_pct:80};
const fetch=async()=>({ok:true,json:async()=>sample});
""" + 'let aiPerformanceBusy=' + section + """
(async()=>{
 await refreshAiPerformance();assert.match(elements.aiLiveState.textContent,/JETSON AI ON/);
 now=400;periodic();assert.doesNotMatch(elements.aiLiveState.textContent,/JETSON AI ON/);
 sample={external_inference_proven:false,model_ready:true,gpu_load_pct:100};
 await refreshAiPerformance();assert.match(elements.aiLiveState.textContent,/AI 준비/);
 assert.doesNotMatch(elements.aiLiveState.textContent,/JETSON AI ON/);
 now=3000;periodic();assert.match(elements.aiLiveMetrics.textContent,/최신 측정값 없음/);
})().catch(error=>{console.error(error);process.exit(1)});
"""
  subprocess.run(['node', '-e', harness], check=True, capture_output=True)



def test_engine_trial_ui_preserves_ssh_failure_code_and_guidance(tmp_path):
  """A rejected Offroad experiment must show its explicit SSH failure reason."""
  path = Path(__file__).resolve().parents[1] / 'features/jetson_pages.py'
  spec = importlib.util.spec_from_file_location('jetson_management_page', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  script = module.PAGE.split('<script>', 1)[1].split('</script>', 1)[0]
  call = 'async function hudUpdateCall(' + script.split('async function hudUpdateCall(', 1)[1].split('async function hudUpdateCheck()', 1)[0]
  harness = """
const assert=require('node:assert/strict');
const token='test-token';
let response={ok:false,status:409,text:async()=>JSON.stringify({
  ok:false,code:'SSH_HOST_KEY_UNKNOWN',
  error:'콤마4 SSH 서버 공개키 지문을 확인하세요.',
  ssh_preflight:{code:'SSH_HOST_KEY_UNKNOWN'}
})};
const fetch=async()=>response;
""" + call + """
(async()=>{
 await assert.rejects(hudUpdateCall('/api/jetson/engine-experiment',{action:'start'}),
   error=>/SSH_HOST_KEY_UNKNOWN/.test(error.message) && /공개키 지문/.test(error.message));
 response={ok:true,status:200,text:async()=>JSON.stringify({ok:true,experiment:{state:'accepted'}})};
 const ok=await hudUpdateCall('/api/jetson/engine-experiment',{action:'status'});
 assert.equal(ok.experiment.state,'accepted');
})().catch(error=>{console.error(error);process.exit(1)});
"""
  script_file = tmp_path / 'ssh-error.js'
  script_file.write_text(harness)
  subprocess.run(['node', str(script_file)], check=True, capture_output=True)
