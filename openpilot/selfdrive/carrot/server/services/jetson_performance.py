"""Comma-side Jetson AI/HUD performance evidence for the unified ZIP export.

Read-only. A button/connection, TensorRT engine, or HUD frame cannot prove
actual external inference. Keep monotonic clock domains on their own hosts.
"""
from __future__ import annotations

from pathlib import Path
import time

from openpilot.selfdrive.carrot.server.services.jetson import age, read_record

MODEL_STATUS = Path('/dev/shm/nexo-jetlink-model.json')
LINK_STATUS = Path('/dev/shm/nexo-jetlink.json')
ENABLE = Path('/data/nexo_jetlink_enabled')
MODEL_NAME = 'NEXO native driving_supercombo'
TIMING_KEYS = ('modeld_roundtrip_ms', 'jetson_execution_us',
               'jetson_prepare_us', 'jetson_total_us', 'completed_inferences')
LEASE_S = 2.25


def collect_comma(now=None):
  now = time.monotonic() if now is None else now
  model = read_record(MODEL_STATUS)
  link = read_record(LINK_STATUS)
  model_age = age(model.get('updated'), now)
  link_age = age(link.get('updated'), now)
  enabled = ENABLE.is_file()
  fresh = model_age is not None and model_age < LEASE_S
  link_fresh = link_age is not None and link_age < 3
  real_inference = (enabled and fresh and model.get('active') is True
                    and model.get('ready') is True and model.get('model') == MODEL_NAME)
  sample_age = age(model.get('last_inference_at_mono'), now)
  sample_recent = fresh and sample_age is not None and sample_age < 5
  timing = ({key: model[key] for key in TIMING_KEYS
             if type(model.get(key)) in (int, float) and 0 <= model[key] < 1e9}
            if sample_recent else {})
  status = ('active' if real_inference else 'disabled' if not enabled else
            'unknown_stale_model' if not fresh else
            'not_active')
  return {'schema_version': 1, 'read_only': True, 'inference_state': status,
          'external_inference_proven': real_inference,
          'enabled_marker': enabled, 'model_status_fresh': fresh,
          'model_status_age_s': round(model_age, 3) if model_age is not None else None,
          'jetlink_status_fresh': link_fresh,
          'jetlink_status_age_s': round(link_age, 3) if link_age is not None else None,
          'model_ready': model.get('ready') is True if fresh else None,
          'model_active': model.get('active') is True if fresh else None,
          'model_name': model.get('model') if fresh else None,
          'link_state': str(link.get('state') or 'unknown')[:70] if link_fresh else 'unknown',
          'link_transport': str(link.get('transport') or 'unknown')[:32] if link_fresh else 'unknown',
          'model_error': str(model.get('error') or '')[:250] if fresh else '',
          'link_error': str(link.get('error') or '')[:250] if link_fresh else '',
          'last_inference_timing_age_s': round(sample_age, 3) if sample_recent else None,
          'timing_is_historical': bool(timing and not real_inference),
          'timing': timing,
          'inference_window': (model.get('inference_window')
                                if sample_recent and isinstance(model.get('inference_window'), dict) else None),
          'note': ('실제 젯슨 AI 추론을 증명하려면 최근 modeld active/ready와 정확한 모델 계약이 모두 필요합니다. '
                   '마지막 추론 지연시간은 종료 후에도 잠시 남을 수 있으므로 현재 작동 여부와 구분합니다.')}


def summarize(comma, remote=None, remote_model=None, remote_status=None):
  """Honest triage summary; never infer causation from a short sample."""
  perf = remote.get('performance') if isinstance(remote, dict) else None
  perf = perf if isinstance(perf, dict) else None
  installed = remote_model.get('installed_model') if isinstance(remote_model, dict) else None
  installed = installed if isinstance(installed, dict) else {}
  status = remote_status.get('status') if isinstance(remote_status, dict) else None
  status = status if isinstance(status, dict) else {}
  cache = status.get('model_cache')
  cache = cache if isinstance(cache, dict) else {}
  model_sha, cache_sha = installed.get('onnx_sha256'), cache.get('sha256')
  # last-loaded cache metadata can be stale; report the mismatch, not a cause.
  cache_mismatch = (isinstance(model_sha, str) and isinstance(cache_sha, str)
                    and len(model_sha) == len(cache_sha) == 64
                    and all(c in '0123456789abcdef' for c in model_sha + cache_sha)
                    and model_sha != cache_sha)
  faults = []
  if cache_mismatch:
    faults.append('젯슨 마지막 모델 캐시 SHA와 설치된 ONNX SHA가 다릅니다. 이전 캐시일 수 있으므로 엔진 재빌드 전 설치·캐시 계약을 확인해 주세요.')
  if not comma.get('external_inference_proven'):
    faults.append('콤마4에서 실제 젯슨 추론 활성(active+ready) 증거가 없습니다.')
  if isinstance(installed.get('matching_engine_files'), list) and not installed['matching_engine_files']:
    faults.append('원격 모델 검사에서 일치하는 TensorRT 엔진 파일이 발견되지 않았습니다.')
  if perf is None:
    faults.append('젯슨 성능 수집 결과가 없습니다. SSH 및 원격 서비스 상태를 확인해 주세요.')
    return {'schema_version': 1, 'inference': comma, 'jetson_performance_collected': False,
            'warnings': faults, 'evidence': {}, 'limitations': [
              '정상 GPU/CPU/GC 추정을 하지 않습니다.', '실제 외부 HUD 점등 여부는 물리 확인이 필요합니다.']}
  clocks = perf.get('clocks') or {}
  gc = perf.get('hud_python_gc') or {}
  if not clocks.get('gpu_available'):
    faults.append('GPU 주파수/부하 드라이버 정보가 확인되지 않았습니다.')
  if gc.get('available') is not True:
    faults.append('HUD Python GC 관측 자료가 없거나 오래되었습니다. 젯슨 HUD 패키지 버전을 확인해 주세요.')
  if perf.get('scheduler_observation_available') is not True:
    faults.append('젯슨 스레드 스케줄러 통계를 읽지 못했습니다. 권한과 서비스 실행 상태를 확인해 주세요.')
  hud = perf.get('hud_pipeline') or {}
  if not hud:
    faults.append('최신 HUD 패널/인코더 추적 자료가 확인되지 않았습니다.')
  evidence = {'comma_inference': comma.get('inference_state'),
              'comma_last_model_roundtrip_ms': (comma.get('timing') or {}).get('modeld_roundtrip_ms'),
              'jetson_installed_onnx_sha256': model_sha if isinstance(model_sha, str) else None,
              'jetson_cached_model_sha256': cache_sha if isinstance(cache_sha, str) else None,
              'jetson_cache_matches_installed_model': False if cache_mismatch else
                                                    True if isinstance(model_sha, str) and model_sha == cache_sha else None,
              'jetson_gpu': clocks.get('gpu', []),
              'jetson_power_mode': perf.get('power_mode', {}),
              'jetson_memory': perf.get('memory', {}),
              'jetson_processes': perf.get('processes', []),
              'jetson_scheduler_threads': perf.get('server_scheduler_threads', []),
              'hud_python_gc': gc,
              'hud_pipeline': hud}
  return {'schema_version': 1, 'inference': comma, 'jetson_performance_collected': True,
          'warnings': faults, 'evidence': evidence,
          'limitations': [
            '두 장비의 monotonic 시계를 서로 직접 빼지 않습니다.',
            'CPU/GPU·스케줄러 1회 표본만으로 과거 HUD 검은 화면 원인을 확정할 수 없습니다.',
            'Python GC는 HUD 프로세스만 측정하며 GPU CUDA 대기 및 native 렌더 지연과 다릅니다.',
            '패널 쓰기 기록은 실제 물리 화면의 점등을 보장하지 않습니다.',
            '진단 기능은 AI 추론을 활성화하거나 모델 엔진을 생성하지 않습니다.']}


def format_text(value):
  inference = value.get('inference') or value
  performance = value.get('evidence') or {}
  rows = ['엑스플러스 · 젯슨 AI/HUD 성능 통합진단 (읽기 전용)',
          'AI 추론 상태: ' + str(inference.get('inference_state', 'unknown')),
          '실제 외부 추론 확인: ' + ('예' if inference.get('external_inference_proven') else '아니요'),
          '마지막 콤마4 추론 왕복시간(ms): ' + str(
            (inference.get('timing') or {}).get('modeld_roundtrip_ms', '확인 불가'))]
  if 'jetson_performance_collected' in value:
    rows.append('젯슨 성능 수집: ' + ('완료' if value['jetson_performance_collected'] else '실패/미수집'))
    rows.extend('주의: ' + warning for warning in value.get('warnings', []))
    if performance:
      rows += ['설치 ONNX SHA: ' + str(performance.get('jetson_installed_onnx_sha256') or '확인 불가'),
               '마지막 캐시 SHA: ' + str(performance.get('jetson_cached_model_sha256') or '확인 불가'),
               'GPU: ' + str(performance.get('jetson_gpu')),
               '전력 모드: ' + str(performance.get('jetson_power_mode')),
               '메모리: ' + str(performance.get('jetson_memory')),
               'HUD GC: ' + str(performance.get('hud_python_gc')),
               'HUD 패널/인코더: ' + str(performance.get('hud_pipeline')),
               '스케줄러 상위 스레드: ' + str(performance.get('jetson_scheduler_threads'))]
    rows += ['검증 한계: ' + item for item in value.get('limitations', [])]
  else:
    rows.append('추론 상태와 처리시간은 실제 최신 파일이 있을 때만 표시합니다.')
  return '\n'.join(rows) + '\n'
