"""Read-only Offroad engine-trial summary for a Jetson unified ZIP.

The ZIP never initiates a build. Experimental engines are launched only through
the existing separately confirmed and guarded Offroad management endpoint.
"""


def evaluate(local, live_offroad, last_start=None, remote_experiments=None, comma_sha=None):
  """Separate a current safety snapshot, the last launch request and remote GPU evidence."""
  local = local if isinstance(local, dict) else {}
  last_start = last_start if isinstance(last_start, dict) else {}
  remote = remote_experiments if isinstance(remote_experiments, dict) else {}
  found = remote.get('ok') is True and isinstance(remote.get('experiments'), dict)
  record = remote['experiments'] if found else {}
  available = record.get('available') is True
  model_sha = record.get('model_sha256') if available else None
  sha_match = comma_sha == model_sha if isinstance(comma_sha, str) and isinstance(model_sha, str) else None
  # A different model's experiment must never be reported as current-model proof.
  gpu_pass = (record.get('native_gpu_verified') is True and sha_match is True) if available else False
  rows = record.get('trials') if isinstance(record.get('trials'), list) else []
  trials = [
    {'name': row.get('name'), 'state': row.get('state'), 'failed_phase': row.get('failed_phase'),
     'failure_category': (row.get('failure_diagnosis') or {}).get('category') if isinstance(row.get('failure_diagnosis'), dict) else None,
     'synthetic_fps': row.get('synthetic_fps'), 'measured_runs': row.get('measured_runs'),
     'synchronized_inference_ms': row.get('synchronized_inference_ms')}
    for row in rows[:12] if isinstance(row, dict)]
  offroad = live_offroad is True and local.get('offroad') is True and local.get('onroad') is False
  enabled = local.get('enabled')
  safe_gate = offroad and enabled is False
  return {
    'schema_version': 1, 'bundle_collection_read_only': True,
    'trial_started_by_bundle': False, 'production_ai_activated_by_bundle': False,
    'vehicle_inference_verified': False,
    'safety': {
      'comma_local_offroad': local.get('offroad'), 'comma_local_onroad': local.get('onroad'),
      'live_params_full_offroad': live_offroad is True,
      'external_jetlink_enabled': enabled,
      'full_offroad_confirmed_at_collection': offroad,
      'eligible_local_gate_at_collection': safe_gate,
      'note': 'ZIP을 저장한 순간의 Offroad 증거입니다. 이전 엔진 시험 시작 당시 상태를 증명하지 않습니다.',
    },
    'launch': {'recorded': bool(last_start), 'accepted': last_start.get('ok') if last_start else None,
               'requested_trial': last_start.get('trial') if last_start else None,
               'time': last_start.get('time') if last_start else None,
               'error': last_start.get('error') if last_start else None},
    'experiment': {'available': available, 'state': record.get('state') if available else None,
                   'current_trial': record.get('current_trial') if available else None,
                   'model_sha256': model_sha, 'comma_model_sha256': comma_sha,
                   'matches_current_comma_model': sha_match,
                   'native_gpu_verified_for_matching_model': gpu_pass,
                   'production_cache_published': False, 'error': record.get('error') if available else None,
                   'trials': trials},
    'note': ('기존 시험 기록만 수집했습니다. 완전 Offroad에서 사용자 확인으로 독립 native_fp16 시험을 시작한 후'
             ' 완료·실패 결과를 통합진단 ZIP에서 다시 확인하십시오. 합성 GPU 성공은 실제 차량 AI 작동의 증거가 아닙니다.'),
  }


def format_text(value):
  safe = value['safety']
  trial = value['experiment']
  launch = value['launch']
  label = lambda v: '예' if v is True else '아니요' if v is False else '미검증'
  lines = [
    'NEXO XPlus · 완전 Offroad AI 엔진 독립 시험 진단',
    'ZIP 생성 중 엔진 빌드: 없음 (읽기 전용)',
    '현재 콤마4 완전 Offroad: ' + label(safe['full_offroad_confirmed_at_collection']),
    '실시간 Offroad 파라미터 확인: ' + label(safe['live_params_full_offroad']),
    '외부 Jetlink 비활성 확인: ' + label(safe['external_jetlink_enabled'] is False if safe['external_jetlink_enabled'] is not None else None),
    '로컬 시험 조건: ' + label(safe['eligible_local_gate_at_collection']),
    '이전 엔진 시험 요청 기록: ' + ('있음' if launch['recorded'] else '없음'),
    '이전 요청 승인: ' + label(launch['accepted']),
    '이전 시험 선택: ' + str(launch['requested_trial'] or '미기록'),
    '젯슨 독립 시험 기록 수집: ' + label(trial['available']),
    '시험 전체 상태: ' + str(trial['state'] or '미검증'),
    '현재 모델 SHA 일치: ' + label(trial['matches_current_comma_model']),
    '현재 모델 합성 GPU 검증: ' + label(trial['native_gpu_verified_for_matching_model']),
    '실제 차량 AI 추론 검증: 아니요',
  ]
  if launch.get('error'):
    lines.append('이전 시험 시작 오류: ' + str(launch['error'])[:240])
  if trial.get('error'):
    lines.append('실험 오류: ' + str(trial['error'])[:240])
  for row in trial['trials']:
    lines.append('ㆍ' + str(row.get('name') or '이름 없음') + ': ' +
                 str(row.get('state') or '미검증') + ' · 단계 ' +
                 str(row.get('failed_phase') or '-') + ' · 실패 분류 ' +
                 str(row.get('failure_category') or '-'))
  lines += ['', safe['note'], value['note']]
  return '\n'.join(lines) + '\n'
