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
  return {'schema_version': 1, 'read_only': True,
          'comma_onroad': local.get('onroad') if type(local.get('onroad')) is bool else None,
          'comma_offroad': local.get('offroad') if type(local.get('offroad')) is bool else None,
          'jetson_readiness_collected': peer is not None,
          'jetson': peer, 'blockers': list(dict.fromkeys(reasons)),
          'can_activate_now': False, 'auto_install_or_build': False,
          'real_vehicle_inference_verified': False,
          'model_hashes_consistent': (peer.get('package_model_matches') is True and
                                      peer.get('ncm_model_matches') is True) if peer else None,
          'engine_build_verified': False,
          'recommendation': ('실제 Offroad 확인 후 보호 설치기로 현재 모델의 TensorRT 엔진 생성과 '
                             '합성 추론 검사를 진행해야 합니다. HUD 전용 연결과 설치된 캐시는 '
                             '자동으로 변경하지 않습니다. P단만으로 전환을 허용하지 않습니다.')}


def format_text(report):
  peer = report.get('jetson') or {}
  return '\n'.join((
    '엑스플러스 AI 전환 준비 진단 (읽기 전용)',
    '콤마4 Onroad: ' + str(report.get('comma_onroad')),
    '콤마4 Offroad: ' + str(report.get('comma_offroad')),
    '젯슨 수집: ' + str(report.get('jetson_readiness_collected')),
    '현재 설치 상태: ' + str(peer.get('install_state', 'unknown')),
    'HUD 전용: ' + str(peer.get('display_only_installed')),
    '현재 모델 엔진 파일 존재: ' + str(peer.get('engine_file_present')),
    '설치/전송 모델 SHA 일치: ' + str(report.get('model_hashes_consistent')),
    '실제 TensorRT 엔진 빌드 검증: ' + str(report.get('engine_build_verified')),
    '기존 캐시 보존 예상: ' + str(peer.get('old_cache_expected_in_hud_only')),
    'USB-NCM 연결 최신: ' + str(peer.get('ncm_fresh_ready')),
    '인터페이스 자동 복구: ' + str(peer.get('ncm_interface_auto_recovered')),
    '미충족 조건: ' + ', '.join(report.get('blockers') or ['없음']),
    'AI 자동 활성화/설치: 수행하지 않음',
    '권장 조치: ' + report['recommendation'],
  )) + '\n'
