"""Read-only AI root-cause summary for the one-click Jetson diagnostics ZIP.

This report is an interpretation of collected records, not a new device probe.
Never convert unobserved GPU inference into 'success' and never modify controls.
"""
from __future__ import annotations

import re

SHA = re.compile(r'[0-9a-f]{64}\Z')
TRIALS = ('native_fp16', 'native_strongly_typed', 'gelu_tanh_primitives',
          'fp32_compute', 'native_no_fp16_flag', 'gelu_fp32_island',
          'region_conv_gelu', 'region_conv', 'region_gelu',
          'native_tactic_level5', 'native_half_workspace')
CATEGORIES = {
  'conv_gelu_tactic_unavailable': 'Conv2D + GELU 연산 구현 선택 실패',
  'tactic_unavailable': 'TensorRT 연산 구현 선택 실패',
  'memory_allocation_failed': 'GPU 메모리 할당 실패 기록',
  'onnx_parse_failed': 'ONNX 모델 해석 실패',
  'unclassified': '분류되지 않은 엔진 생성 오류',
}
STAGES = {
  'remote_unavailable': '젯슨 AI 자료 미수집',
  'model_mismatch': 'AI 모델 일치 검증 실패',
  'engine_build_failed': 'TensorRT 엔진 생성 실패 기록',
  'engine_missing': '현재 모델용 TensorRT 엔진 없음',
  'engine_unverified': '현재 모델용 엔진 GPU 검증 미완료',
  'runtime_not_ready': 'AI 런타임 설치·활성 조건 미충족',
  'vehicle_unverified': '실제 차량 추론 미검증',
  'vehicle_observed': '실제 추론 결과 보고됨 · 추가 검증 필요',
}


def _payload(value, name):
  return value.get(name) if isinstance(value, dict) and value.get('ok') is True and isinstance(value.get(name), dict) else None


def _sha(value):
  return value if isinstance(value, str) and SHA.fullmatch(value) else None


def _category(value):
  return value if value in CATEGORIES else 'unclassified'


def evaluate(comma_local, comma_ai, remote_readiness, remote_model, remote_experiments,
             remote_ssh=None, comma_model_sha=None, lte_gateway=None):
  """Summarize already collected values. Never execute commands or mutate inputs."""
  local = comma_local if isinstance(comma_local, dict) else {}
  local_ai = comma_ai if isinstance(comma_ai, dict) else {}
  peer = _payload(remote_readiness, 'inference_readiness')
  model = (remote_model.get('installed_model') if isinstance(remote_model, dict)
           and remote_model.get('ok') is True and isinstance(remote_model.get('installed_model'), dict) else {})
  experiment = _payload(remote_experiments, 'experiments')
  remote_sha = _sha(peer.get('installed_sha256')) if peer else None
  comma_sha = _sha(comma_model_sha)
  spec_sha = _sha((model.get('package_contract') or {}).get('sha256'))
  model_match = comma_sha == remote_sha if comma_sha and remote_sha else None
  model_record_sha = _sha(model.get('onnx_sha256'))
  actual_mismatch = (model_match is False or
                     bool(remote_sha and spec_sha and remote_sha != spec_sha) or
                     bool(remote_sha and model_record_sha and remote_sha != model_record_sha))

  # A build error for another model must not be reported as today's root cause.
  errors = peer.get('engine_build_errors', []) if peer else []
  current_errors = [row for row in errors if isinstance(row, dict) and remote_sha
                    and str(row.get('file', '')).startswith(remote_sha[:16] + '.')]
  categories = [_category((row.get('failure_diagnosis') or {}).get('category'))
                for row in current_errors if isinstance(row.get('failure_diagnosis'), dict)]
  latest_error = current_errors[-1] if current_errors else None
  status = (experiment.get('state') if experiment else None)
  matrix_sha = _sha(experiment.get('model_sha256')) if experiment else None
  same_matrix = bool(remote_sha and matrix_sha == remote_sha)
  trials = []
  if same_matrix:
    excerpts = {row.get('trial'): row for row in experiment.get('failed_build_excerpts', [])
                if isinstance(row, dict) and row.get('trial') in TRIALS}
    for row in (experiment.get('trials') or [])[:12]:
      if not isinstance(row, dict) or row.get('name') not in TRIALS:
        continue
      diagnosis = row.get('failure_diagnosis')
      if not isinstance(diagnosis, dict):
        diagnosis = excerpts.get(row['name'], {}).get('failure_diagnosis') or {}
      trials.append({'name': row['name'], 'state': str(row.get('state') or 'unknown')[:30],
                     'failure_category': _category(diagnosis.get('category')) if diagnosis else None,
                     'failed_phase': str(row.get('failed_phase') or '')[:60] or None,
                     'reference_status': str(row.get('reference_status') or '')[:40] or None,
                     'reference_error': str(row.get('reference_error') or '')[:160] or None,
                     'verbose_log_available': bool(row.get('verbose_log_path')),
                     'synthetic_fps': row.get('synthetic_fps') if type(row.get('synthetic_fps')) in (int, float) else None,
                     'measured_runs': row.get('measured_runs') if type(row.get('measured_runs')) is int else None})
  root_cause = 'not_established'
  if current_errors:
    root_cause = categories[-1] if categories else 'unclassified'
  elif same_matrix:
    bad = [row['failure_category'] for row in trials if row['state'] == 'failed' and row['failure_category']]
    if bad:
      root_cause = bad[0]
  blockers = [v[:80] for v in (peer.get('blockers') or [])[:20] if isinstance(v, str)] if peer else []
  engine_present = peer.get('engine_file_present') is True if peer else False
  engine_verified = peer.get('synthetic_inference_verified') is True if peer else False
  external = local_ai.get('external_inference_proven') is True
  if peer is None:
    stage = 'remote_unavailable'
  elif actual_mismatch:
    stage = 'model_mismatch'
  elif latest_error and not engine_verified:
    stage = 'engine_build_failed'
  elif not engine_present:
    stage = 'engine_missing'
  elif not engine_verified:
    stage = 'engine_unverified'
  elif peer.get('display_only_installed') is True or peer.get('install_state') != 'installed':
    stage = 'runtime_not_ready'
  elif not external:
    stage = 'vehicle_unverified'
  else:
    stage = 'vehicle_observed'

  next_checks = []
  if peer is None:
    next_checks.append('콤마4 → 젯슨 SSH 연결을 확인하고 통합진단을 다시 수집하세요.')
  else:
    if model_match is False:
      next_checks.append('콤마4와 젯슨의 모델 SHA가 다릅니다. 배포 모델·계약부터 비교하세요.')
    if peer.get('package_model_matches') is False or peer.get('ncm_model_matches') is False:
      next_checks.append('젯슨 ONNX·배포 계약·USB 전송 모델의 불일치 또는 자료 누락을 구분하세요.')
    if root_cause == 'conv_gelu_tactic_unavailable':
      next_checks.append('Offroad에서 Conv/GELU 단일 영역의 FP16·FP32 실험을 비교하고 TensorRT VERBOSE 로그를 확인하세요.')
    elif root_cause != 'not_established':
      next_checks.append('Offroad에서 이전 빌드 로그의 연산·메모리·tactic 거절 원인을 확인하세요.')
    if not engine_verified:
      next_checks.append('엔진 시험은 IsOffroad=1·IsOnroad=0 및 안전한 SSH 경로가 확인된 경우에만 실행하세요.')
    if not same_matrix:
      next_checks.append('현재 모델의 독립 시험 기록이 없습니다. Offroad 전환 후 native_fp16부터 시작하고 이어 native_no_fp16_flag·fp32_compute·region_conv_gelu을 비교하세요. P단 Onroad에서는 읽기 전용 진단만 가능합니다.')
    elif trials and any(row['state'] == 'failed' for row in trials):
      next_checks.append('실패한 시험의 단계와 VERBOSE 로그에서 거절된 tactic을 비교하세요. 메모리 부족으로 단정하지 마세요.')
    if 'USB_NCM_NOT_FRESH_READY' in blockers:
      next_checks.append('USB-NCM 최근 응답과 역방향 SSH 인증은 별도로 점검하세요.')
    if 'FREE_SPACE_BELOW_OR_UNKNOWN' in blockers:
      next_checks.append('젯슨 DATA 여유 공간이 4GiB 이상인지 확인하세요.')
    if peer.get('display_only_installed') is True:
      next_checks.append('현재 HUD 전용 설치입니다. GPU 검증 후 AI 런타임 전환 여부를 별도로 결정하세요.')
  reverse = remote_ssh.get('jetson_to_comma') if isinstance(remote_ssh, dict) else None
  reverse_client = reverse.get('client') if isinstance(reverse, dict) else None
  credentials = reverse.get('credential_preflight') if isinstance(reverse, dict) else None
  if not isinstance(credentials, dict) and isinstance(reverse_client, dict):
    credentials = {
      'identity_candidate_present': any(k.get('exists') and k.get('readable')
                                        for k in reverse_client.get('identity_files', []) if isinstance(k, dict)),
      'ssh_agent_present': reverse_client.get('agent_available') is True,
      'known_hosts_present': any(k.get('exists') for k in reverse_client.get('known_hosts', []) if isinstance(k, dict)),
    }
  if not isinstance(credentials, dict):
    credentials = {}
  reverse_fresh = remote_ssh.get('reverse_result_fresh') is True if isinstance(remote_ssh, dict) else False
  if isinstance(reverse, dict) and reverse.get('code') == 'USB_NCM_UNAVAILABLE':
    next_checks.append('젯슨 → 콤마4 직접 USB 경로가 실패했습니다. 진단의 실제 dev/src와 기대 주소를 비교하고 NCM IP·carrier를 확인하세요. 경로를 자동 변경하지 마세요.')
  if isinstance(reverse, dict) and reverse.get('code') == 'SSH_HOST_KEY_UNKNOWN':
    next_checks.append('USB-NCM은 별개입니다. 젯슨 SSH 실행 계정의 known_hosts에 콤마4 호스트 키가 승인되지 않았습니다. 장치에서 실제 호스트 공개키 지문을 별도로 확인한 뒤 신뢰 등록하세요. StrictHostKeyChecking을 끄지 마세요.')
  if isinstance(reverse, dict) and credentials.get('identity_candidate_present') is False and credentials.get('ssh_agent_present') is False:
    next_checks.append('젯슨 실행 계정에 기존 SSH 개인키·agent가 없습니다. 승인된 키와 known_hosts를 장치에서 안전하게 설정해야 합니다. 자동 키 생성은 하지 않습니다.')
  if isinstance(reverse, dict) and reverse.get('ok') is not True and not reverse_fresh:
    next_checks.append('역방향 SSH 기록이 최신 검증 결과가 아닙니다. 통신 연결 후 SSH 확인을 다시 실행하세요.')
  lte = lte_gateway if isinstance(lte_gateway, dict) else {}
  lte_failures = sorted({str(row.get('code'))[:80] for row in lte.get('failures', [])
                         if isinstance(row, dict) and isinstance(row.get('code'), str)})
  if 'ACTIVE_CELLULAR_WAN_UNVERIFIED' in lte_failures or 'CELLULAR_UPLINK_UNVERIFIED' in lte_failures:
    next_checks.append('LTE 인터넷 공유는 회선 확인 실패입니다. 콤마4 기본 경로와 LTE 모뎀·PPP table 1000을 구분해서 점검하세요. Wi-Fi를 LTE로 잘못 공유하지 마세요.')
  next_checks.append('진단 자체는 엔진 생성·AI 활성화·차량 제어·네트워크 변경을 하지 않습니다.')

  return {
    'schema_version': 2, 'read_only': True, 'engine_build_performed': False,
    'activation_performed': False, 'real_vehicle_inference_verified_by_report': False,
    'stage_code': stage, 'stage_label': STAGES[stage],
    'root_cause_category': root_cause, 'root_cause_proven': False,
    'failure_classification': CATEGORIES.get(root_cause, '원인 확정되지 않음'),
    'model': {'comma_sha256': comma_sha, 'jetson_sha256': remote_sha, 'package_sha256': spec_sha,
              'jetson_onnx_bytes': model.get('onnx_bytes') if model else None,
              'remote_model_record_sha256': model_record_sha,
              'comma_jetson_sha_match': model_match,
              'package_model_matches': peer.get('package_model_matches') if peer else None,
              'ncm_model_matches': peer.get('ncm_model_matches') if peer else None},
    'runtime': {'package_runtime': model.get('package_runtime') if model else None,
                'builder_fingerprint': model.get('builder_fingerprint') if model else None,
                'installed_packages': str(model.get('installed_packages') or '')[:900],
                'installed_runtime_state': peer.get('install_state') if peer else None,
                'hud_only': peer.get('display_only_installed') if peer else None},
    'engine': {'matching_file_present': peer.get('engine_file_present') if peer else None,
               'gpu_synthetic_verified': peer.get('synthetic_inference_verified') if peer else None,
               'native_experiment_verified': peer.get('native_gpu_experiment_verified') if peer else None,
               'old_cache_sha256': _sha(peer.get('last_loaded_sha256')) if peer else None,
               'latest_current_model_failure_file': str(latest_error.get('file'))[:120] if latest_error else None,
               'current_model_failure_count': len(current_errors),
               'failure_categories': categories,
               'last_build_context': {key: latest_error.get(key) for key in
                                      ('trt_version', 'device', 'workspace_bytes', 'available_bytes',
                                       'fp16', 'optimization_level', 'failed_phase', 'elapsed_seconds')
                                      if latest_error and key in latest_error}},
    'experiments': {'collected': experiment is not None,
                    'current_model_results': same_matrix, 'state': status,
                    'model_sha256': matrix_sha, 'trials': trials,
                    'proposed_comparison': ['native_fp16', 'native_no_fp16_flag', 'fp32_compute',
                                            'region_conv_gelu', 'region_conv', 'region_gelu']},
    'connectivity': {'ncm_fresh_ready': peer.get('ncm_fresh_ready') if peer else None,
                     'reverse_ssh_verified': reverse.get('ok') if isinstance(reverse, dict) else None,
                     'reverse_ssh_result_fresh': reverse_fresh,
                     'reverse_ssh_failure_code': reverse.get('code') if isinstance(reverse, dict) else None,
                     'reverse_ncm_reason': reverse.get('ncm_reason') if isinstance(reverse, dict) else None,
                     'reverse_route_observed': reverse.get('route_observed') if isinstance(reverse, dict) else None,
                     'reverse_identity_available': credentials.get('identity_candidate_present'),
                     'reverse_agent_available': credentials.get('ssh_agent_present'),
                     'reverse_known_hosts_present': credentials.get('known_hosts_present')},
    'lte_gateway': {'configured': lte.get('configured') if type(lte.get('configured')) is bool else None,
                    'operational': lte.get('operational') if type(lte.get('operational')) is bool else None,
                    'failure_codes': lte_failures,
                    'default_interface': (lte.get('uplink_diagnostics') or {}).get('default_interface')
                      if isinstance(lte.get('uplink_diagnostics'), dict) else None},
    'comma_offroad': local.get('offroad') if type(local.get('offroad')) is bool else None,
    'comma_onroad': local.get('onroad') if type(local.get('onroad')) is bool else None,
    'external_inference_reported': local_ai.get('external_inference_proven') if type(local_ai.get('external_inference_proven')) is bool else None,
    'blockers': blockers, 'next_checks': next_checks,
    'note': '이전 실패 기록은 원인을 확정하지 않습니다. GPU 부하나 HUD 출력만으로 AI 추론 성공을 판단하지 않습니다.',
  }


def format_text(report):
  model, engine, experiment = report['model'], report['engine'], report['experiments']
  lines = ['엑스플러스 젯슨 AI 집중 진단 · 읽기 전용',
           '진단 단계: ' + report['stage_label'] + ' (' + report['stage_code'] + ')',
           '실패 분류: ' + report['failure_classification'] + ' · 근본 원인 미확정',
           '콤마4 모델 SHA: ' + str(model['comma_sha256']),
           '젯슨 모델 SHA: ' + str(model['jetson_sha256']),
           '콤마4·젯슨 모델 일치: ' + str(model['comma_jetson_sha_match']),
           '젯슨 설치 모델 크기(바이트): ' + str(model['jetson_onnx_bytes']),
           '젯슨 실행 환경: ' + str(report['runtime']['package_runtime']),
           '설치된 당근 TensorRT 빌더 SHA: ' + str((report['runtime']['builder_fingerprint'] or {}).get('sha256')),
           '현재 모델 엔진 파일: ' + str(engine['matching_file_present']),
           'GPU 합성 추론 검증: ' + str(engine['gpu_synthetic_verified']),
           '현재 모델 빌드 실패 기록: ' + str(engine['current_model_failure_count']) + '개',
           '최근 빌드 환경: ' + str(engine['last_build_context']),
           '독립 엔진 실험: ' + str(experiment['state'] if experiment['current_model_results'] else '현재 모델 기록 없음'),
           '최근 USB-NCM 준비: ' + str(report['connectivity']['ncm_fresh_ready']),
           '역방향 SSH 검증: ' + str(report['connectivity']['reverse_ssh_verified']),
           '역방향 SSH 오류: ' + str(report['connectivity']['reverse_ssh_failure_code'])
             + ' · USB: ' + str(report['connectivity']['reverse_ncm_reason'])
             + ' · 최근 검사: ' + str(report['connectivity']['reverse_ssh_result_fresh']),
           '역방향 SSH 키 후보/agent/known_hosts: ' + str(report['connectivity']['reverse_identity_available'])
             + '/' + str(report['connectivity']['reverse_agent_available'])
             + '/' + str(report['connectivity']['reverse_known_hosts_present']),
           'LTE 공유: ' + str(report['lte_gateway']['operational'])
             + ' · 기본 경로: ' + str(report['lte_gateway']['default_interface'])
             + ' · 실패 코드: ' + ', '.join(report['lte_gateway']['failure_codes']),
           '실차 추론 보고: ' + str(report['external_inference_reported']) + ' · 실차 검증과 별개',
           '미충족 조건: ' + ', '.join(report['blockers'] or ['확인되지 않음']),
           '', '실험 결과']
  lines.extend('  ' + row['name'] + ': ' + row['state']
               + (' · 실패 단계 ' + row['failed_phase'] if row['failed_phase'] else '')
               + (' · ' + row['failure_category'] if row['failure_category'] else '')
               + (' · GPU 합성 통과 / CPU 비교 미완료' if row['state'] == 'gpu_synthetic_passed' and row['reference_status'] == 'unavailable' else '')
               + (' · 수치 비교 기록 있음 / 차량 사용 승인 아님' if row['reference_status'] == 'compared_unapproved' else '')
               + (' · VERBOSE 로그 존재' if row['verbose_log_available'] else '')
               for row in experiment['trials'])
  if not experiment['trials']:
    lines.append('  현재 모델의 독립 실험 기록 없음')
  lines.extend(['', '권장 점검'])
  lines.extend('  ' + str(index) + '. ' + note for index, note in enumerate(report['next_checks'], 1))
  lines.append('※ 모델·엔진·주행 설정은 변경하지 않았습니다.')
  return '\n'.join(lines) + '\n'
