"""HUD management stage translation is presentation-only; backend states stay stable."""
from openpilot.selfdrive.carrot.server.features.jetson_pages import PAGE


def test_all_update_stages_have_korean_labels():
  stages = {
    'idle': '대기',
    'checking': '업데이트 준비 및 확인 중',
    'downloading': '업데이트 파일 다운로드 중',
    'transferring': '젯슨으로 파일 전송 중',
    'preflight': '설치 전 안전 점검 중',
    'installing': '젯슨에 설치 중',
    'verifying': '설치 결과 확인 중',
    'completed': '설치 완료',
    'failed': '설치 실패',
    'unknown': '진행 상태 확인 필요',
  }
  for machine_stage, korean_label in stages.items():
    assert f"{machine_stage}:'{korean_label}'" in PAGE


def test_both_update_status_views_translate():
  assert "'작업 상태: '+hudUpdateStageName((d.job||{}).stage||'idle')" in PAGE
  assert "'진행 단계: '+hudUpdateStageName(j.stage)" in PAGE


def test_machine_states_still_drive_safety_controls():
  # Don't translate JSON or change the stages used for disabling install.
  assert "['completed','failed'].includes(j.stage)" in PAGE
  assert "j.stage==='unknown'" in PAGE
