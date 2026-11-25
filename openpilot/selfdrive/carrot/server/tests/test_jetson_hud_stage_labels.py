"""HUD management stage translation is presentation-only; backend states stay stable."""
from pathlib import Path

# Read the static HTML source directly so this UI-only test does not import
# the full features package (which needs compiled msgq on a running comma).
PAGE = (Path(__file__).resolve().parents[1] / 'features' / 'jetson_pages.py').read_text(encoding='utf-8')


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
  # The UI tracks previous installs separately so old success is not shown as a new update.
  assert 'const previous=d.job||{};' in PAGE
  assert "'작업 상태: '+(priorFailure?'이전 설치 실패 이력 · 현재 버전은 최신 (서비스 별도 확인)':" in PAGE
  assert "const priorFailure=previous.stage==='failed'&&hudUpdateCommit&&hudUpdateInstalled===hudUpdateCommit;" in PAGE
  assert "j.stage==='failed'&&hudUpdateCommit&&hudUpdateInstalled===hudUpdateCommit" in PAGE
  assert "'진행 단계: '+hudUpdateStageName(j.stage)" in PAGE


def test_machine_states_still_drive_safety_controls():
  # Don't translate JSON or change the stages used for disabling install.
  assert "['completed','failed'].includes(j.stage)" in PAGE
  assert "j.stage==='unknown'" in PAGE


def test_unpublished_release_auto_rechecks_without_installing():
  assert "d.release_status==='not_published'" in PAGE
  assert "'배포 대기 · 메인 '" in PAGE
  assert "자동 재확인: 이 페이지가 열려 있으면 5분 간격으로 검증 패키지를 다시 확인합니다." in PAGE
  assert '},300000);' in PAGE
  assert "setInterval(()=>{if(hudUpdateRecheck&&token" in PAGE
  assert "document.visibilityState!=='hidden'" in PAGE
  assert "&&d.release_ready===true&&d.available===true" in PAGE


def test_unpublished_release_rechecks_when_tab_returns_without_rapid_requests():
  # An existing validated release is rechecked only after returning to the page;
  # no foreground handler starts an install or changes the vehicle safety gate.
  assert 'hudUpdateLastCheckAt=Date.now();' in PAGE
  assert 'hudUpdateOnResume=unpublished||compatibleFallback||rateLimited;' in PAGE
  assert "hudUpdateOnResume?['화면 복귀 시:" in PAGE
  assert 'Date.now()-hudUpdateLastCheckAt>=120000' in PAGE
  assert "document.addEventListener('visibilitychange',hudUpdateCheckOnResume);" in PAGE
  assert "window.addEventListener('focus',hudUpdateCheckOnResume);" in PAGE
  assert "hudUpdateOnResume&&token&&!hudUpdateChecking" in PAGE
  assert "hudUpdateRateLimitReset" not in PAGE
  assert "REST 요청 제한 중에도 공식 Git 경로를 5분 간격으로 확인합니다." in PAGE
  assert "&&d.release_ready===true&&d.available===true" in PAGE


def test_lte_buttons_follow_configuration_and_live_route_evidence():
  # Offroad is a safety prerequisite, but it does not mean the gateway is
  # configured, and an installed firewall rule does not prove usable LTE.
  assert "lteConfigured=v.configured===true;" in PAGE
  assert "lteProbeReady=v.operational===true;" in PAGE
  assert "$('lteEnable').disabled=!lteChangeAllowed||lteConfigured;" in PAGE
  assert "$('lteDisable').disabled=!lteChangeAllowed||!lteConfigured;" in PAGE
  assert "$('lteProbe').disabled=!lteChangeAllowed||!lteProbeReady;" in PAGE
  assert "if(action!=='status'){" in PAGE
  assert "lteChangeAllowed=false;lteConfigured=false;lteProbeReady=false;" in PAGE
