"""Read-only and fail-closed NEXO AI offload preparation assessment."""


def evaluate(comma_local, remote):
  local = comma_local if isinstance(comma_local, dict) else {}
  peer = remote.get('inference_readiness') if isinstance(remote, dict) and remote.get('ok') is True else None
  if not isinstance(peer, dict):
    peer = None
  offroad = local.get('offroad') is True and local.get('onroad') is False
  reasons = []
  if not offroad:
    reasons.append('COMMA_OFFROAD_NOT_VERIFIED')
  if local.get('enabled') is True:
    reasons.append('COMMA_INFERENCE_ALREADY_ENABLED')
  if peer is None:
    reasons.append('JETSON_READINESS_UNAVAILABLE')
  else:
    blockers = peer.get('blockers')
    if isinstance(blockers, list):
      reasons.extend(v[:80] for v in blockers[:12] if isinstance(v, str))
    if peer.get('package_model_matches') is False:
      reasons.append('PACKAGE_MODEL_SHA_MISMATCH')
    if peer.get('ncm_model_matches') is False:
      reasons.append('NCM_MODEL_SHA_MISMATCH')
    if peer.get('engine_file_present') is False:
      reasons.append('CURRENT_TRT_ENGINE_NOT_BUILT')
    if peer.get('display_only_installed') is True:
      reasons.append('HUD_ONLY_RUNTIME')
  # Explicitly separate model/build blockers from connectivity and parking.
  # This is read-only: never infer successful inference from a healthy HUD.
  explanations = {
    'COMMA_OFFROAD_NOT_VERIFIED': '콤마4가 완전한 Offroad 상태인지 확인해야 합니다. P단만으로는 충분하지 않습니다.',
    'COMMA_INFERENCE_ALREADY_ENABLED': '중복 활성화를 시도하지 않고 현재 실행 상태를 확인해야 합니다.',
    'JETSON_READINESS_UNAVAILABLE': '젯슨의 최신 준비 진단을 먼저 수집해야 합니다.',
    'HUD_ONLY_RUNTIME': '현재 설치는 HUD 전용이므로 AI 런타임 전환을 별도로 검증해야 합니다.',
    'CURRENT_TRT_ENGINE_NOT_BUILT': '설치된 ONNX 모델과 일치하는 TensorRT 엔진 생성 및 검증이 필요합니다.',
  }
  blockers = list(dict.fromkeys(reasons))
  next_checks = [explanations.get(reason, '젯슨 진단의 차단 조건을 확인해야 합니다: ' + reason)
                 for reason in blockers]
  recommendation = '진단의 미충족 조건을 먼저 확인하세요.'
  if peer and peer.get('engine_build_verified') is True:
    recommendation = '원본 엔진의 합성 검증을 마쳤습니다. 실제 외부 추론 여부는 완료 프레임과 왕복시간에서 확인하세요.'
  elif (peer and peer.get('display_only_installed') is True and peer.get('model_file_ready') is True
        and local.get('enabled') is False and peer.get('engine_file_present') is False
        and set(blockers) <= {'COMMA_OFFROAD_NOT_VERIFIED', 'HUD_ONLY_RUNTIME', 'CURRENT_TRT_ENGINE_NOT_BUILT'}):
    recommendation = ('콤마가 Offroad=1, Onroad=0일 때 Jetson의 prepare_engine.py로 원본 엔진을 준비하세요. '
                      '성공하면 engine_ready와 합성 추론 결과를 확인하세요.')
    sha = peer.get('installed_sha256')
    errors = peer.get('engine_build_errors')
    if (isinstance(sha, str) and len(sha) == 64 and isinstance(errors, list)
        and any(isinstance(row, dict) and str(row.get('file', '')).startswith(sha[:16] + '.')
                and isinstance(row.get('failure_diagnosis'), dict)
                and row['failure_diagnosis'].get('category') == 'conv_gelu_tactic_unavailable' for row in errors)):
      recommendation = ('현재 모델의 이전 빌드에서 Conv/GELU 구현 선택이 실패했습니다. '
                        'IsOffroad=1, IsOnroad=0에서 7000 젯슨 관리의 ‘Offroad 엔진 시험 시작’을 누르세요 (prepare_engine.py --experiment). '
                        '통합 진단 ZIP에서 native_fp16과 native_strongly_typed의 결과를 확인하세요.')
  return {'schema_version': 1, 'read_only': True,
          'comma_onroad': local.get('onroad') if type(local.get('onroad')) is bool else None,
          'comma_offroad': local.get('offroad') if type(local.get('offroad')) is bool else None,
          'jetson_readiness_collected': peer is not None,
          'jetson': peer, 'blockers': blockers, 'next_checks': next_checks,
          'can_activate_now': False, 'auto_install_or_build': False,
          'real_vehicle_inference_verified': False,
          'model_hashes_consistent': (peer.get('package_model_matches') is True and
                                      peer.get('ncm_model_matches') is True) if peer else None,
          'engine_build_verified': peer.get('engine_build_verified') is True if peer else False,
          'synthetic_inference_verified': peer.get('synthetic_inference_verified') is True if peer else False,
          'recommendation': recommendation}


def format_text(report):
  peer = report.get('jetson') or {}
  return '\n'.join((
    '엑스플러스 AI 전환 준비 진단 (읽기 전용)',
    '콤마4 Onroad: ' + str(report.get('comma_onroad')),
    '콤마4 Offroad: ' + str(report.get('comma_offroad')),
    '젯슨 수집: ' + str(report.get('jetson_readiness_collected')),
    '현재 설치 상태: ' + str(peer.get('install_state', 'unknown')),
    'HUD 전용: ' + str(peer.get('display_only_installed')),
    '모델 파일 SHA 준비: ' + str(peer.get('model_file_ready')),
    '현재 모델 엔진 파일 존재: ' + str(peer.get('engine_file_present')),
    '설치/전송 모델 SHA 일치: ' + str(report.get('model_hashes_consistent')),
    '실제 TensorRT 엔진 빌드 검증: ' + str(report.get('engine_build_verified')),
    '운영 엔진 GPU 합성 검증: ' + str(report.get('synthetic_inference_verified')),
    '독립 실험의 원본 GPU 검증: ' + str(peer.get('native_gpu_experiment_verified')) + ' · 운영 캐시 게시와 별개',
    '기존 캐시 보존 예상: ' + str(peer.get('old_cache_expected_in_hud_only')),
    'USB-NCM 연결 최신: ' + str(peer.get('ncm_fresh_ready')),
    '인터페이스 자동 복구: ' + str(peer.get('ncm_interface_auto_recovered')),
    '미충족 조건: ' + ', '.join(report.get('blockers') or ['없음']),
    '다음 점검: ' + ' / '.join(report.get('next_checks') or ['없음']),
    'AI 자동 활성화/설치: 수행하지 않음',
    '권장 조치: ' + report['recommendation'],
  )) + '\n'
