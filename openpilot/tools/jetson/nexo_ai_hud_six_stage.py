"""Fail-closed six-step NEXO AI-to-HUD evidence receipt.

Read-only composition of existing diagnostic records. The report never
changes GPU, CAN, camera, modeld, physical display, or runtime state.
"""
from __future__ import annotations


def _obj(v):
  return v if isinstance(v, dict) else {}


def n16_execution_verified(single_trial, gpu):
  from openpilot.tools.jetson.nexo_n16_registry import TRUSTED_SHA256
  single_trial = _obj(single_trial)
  gpu = _obj(gpu)
  if not gpu.get('state'):
    return (single_trial.get('model_sha256') == TRUSTED_SHA256 and single_trial.get('state') == 'passed'
            and single_trial.get('gpu_inference_verified') is True)
  return (gpu.get('model_sha256') == TRUSTED_SHA256 and gpu.get('cut_node_index') == 360
          and gpu.get('state') in ('gpu_parity_failed', 'gpu_parity_passed')
          and gpu.get('phase') == 'offline_gpu_replay_complete' and gpu.get('frames_completed') == 140
          and gpu.get('gpu_inference_verified') is True)


def evaluate(update, comma_status, readiness, n16_status, function_status=None, split_gpu=None):
  update, comma, readiness, n16, function = map(
    _obj, (update, comma_status, readiness, n16_status, function_status))
  jetson = _obj(readiness.get('jetson'))
  n16_trial = _obj(n16.get('trial'))
  n16_hud = _obj(n16.get('hud_observer'))
  pipeline = _obj(comma.get('hud_pipeline'))

  installed = (update.get('stage') == 'completed'
               and update.get('installed_matches_target') is True)
  transport = (comma.get('connected') is True
               and comma.get('display_hello_verified') is True
               and comma.get('hud_tx_recent') is True)
  # Step 3 applies to the installed standard ONNX, not the separate N16 source.
  standard_onnx = (jetson.get('model_file_ready') is True
                   and jetson.get('package_model_matches') is True
                   and jetson.get('ncm_model_matches') is True)
  split_gpu = _obj(split_gpu)
  n16_gpu = n16_execution_verified(n16_trial, split_gpu)
  # N16 numerical parity, NEXO camera geometry, actual drawable overlay and
  # physical panel acceptance have no independent trustworthy receipt here.
  # A reported flag is NEVER enough to turn either of these steps green.
  ai_render_verified = False
  physical_verified = False
  descriptions = (
    ('젯슨 HUD 검증 패키지 설치', installed, '설치 커밋과 검증된 업데이트 대상 일치 필요'),
    ('콤마4·젯슨 HUD 통신', transport, 'USB-NCM HELLO 및 최근 HUD 데이터 송신 확인 필요'),
    ('기본 ONNX 모델 파일·해시 일치', standard_onnx,
     '기본 모델 SHA256·NCM 계약 확인 필요 · Carrot N16 모델과 별개'),
    ('Carrot N16 TensorRT GPU 합성 실행', n16_gpu,
     '독립 Offroad N16 엔진 생성 및 합성 GPU 추론 검증 필요'),
    ('검증된 N16 차선·차량 결과의 HUD 투영', ai_render_verified,
     '원본 수치 동등성·넥쏘 실영상·HUD 투영 좌표·표시 경로 검증 필요'),
    ('외부 HUD 실제 화면 출력 확인', physical_verified,
     '실제 HUD를 연결하고 화면을 육안 확인해야 하며 로그만으로 검증 불가'),
  )
  steps = [{'step': i, 'name': title, 'passed': passed,
            'state': 'verified' if passed else 'not_verified',
            'next_required': None if passed else next_required}
           for i, (title, passed, next_required) in enumerate(descriptions, 1)]
  return {
    'schema_version': 1, 'source': 'existing_read_only_jetson_unified_diagnostics',
    'vehicle': 'Hyundai NEXO (FE), 2019',
    'research_model': 'Carrot N16 (separate from standard ONNX)',
    'read_only': True, 'model_or_hud_activated': False,
    'driving_control_enabled_by_report': False,
    'steps': steps, 'verified_steps': sum(step['passed'] for step in steps),
    'physical_hud_accepted': False,
    'observations': {
      'n16_trial_state': n16_trial.get('state'), 'n16_split_gpu_state': split_gpu.get('state'),
      'n16_last_phase': n16_trial.get('phase'),
      'n16_failure': str(n16_trial.get('error') or '')[:240],
      'n16_physical_feed_reported': n16_hud.get('physical_hud_feed_enabled') is True,
      'hud_panel_write_recent': pipeline.get('panel_write_recent') is True,
      'hud_renderer_running': pipeline.get('renderer_running') is True,
      'previous_physical_hud_flag': function.get('physical_hud_verified') is True,
    },
    'notes': [
      '기본 ONNX SHA 일치는 Carrot N16 모델 추론의 증거가 아닙니다.',
      '합성 GPU 출력만으로 넥쏘 실영상의 차량·차선 인식 정확도를 보장하지 않습니다.',
      '패널 송신과 렌더러 상태는 실제 화면 점등을 증명하지 않습니다.',
      '모델·CAN·운영 HUD를 변경하거나 자동으로 활성화하지 않습니다.',
    ],
  }


def format_text(report):
  lines = ['NEXO AI → 외부 HUD 6단계 검증 (읽기 전용)',
           f"검증 통과: {report['verified_steps']}/6"]
  for stage in report['steps']:
    lines.append(f"{stage['step']}. {stage['name']}: {'확인됨' if stage['passed'] else '미검증'}")
    if not stage['passed']:
      lines.append('   필요: ' + stage['next_required'])
  obs = report['observations']
  lines.extend((f"N16 분할 GPU 시험: {obs.get('n16_split_gpu_state') or '기록 없음'}",
                f"N16 단일 엔진 시험 기록: {obs.get('n16_trial_state') or '미확인'} / {obs.get('n16_last_phase') or '미확인'}",
                f"N16 단일 엔진 오류 기록: {obs.get('n16_failure') or '기록 없음'}",
                '실제 HUD 물리 출력: 미검증 · 현장 육안 확인 필요'))
  return '\n'.join(lines) + '\n'
