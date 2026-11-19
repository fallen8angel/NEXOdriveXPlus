"""Keep the Jetson 7000 screen compact without removing existing controls."""
from pathlib import Path
PAGE = (Path(__file__).resolve().parents[1] / 'features' / 'jetson_pages.py').read_text(encoding='utf-8')


def test_five_views_are_named_and_summary_is_default():
  labels = {'overview': '요약', 'update': '업데이트', 'ai': 'AI·HUD',
            'diagnostics': '진단', 'connection': '연결·설정'}
  for key, label in labels.items():
    assert f'data-jetson-tab="{key}"' in PAGE
    assert f'>{label}</button>' in PAGE
  assert "setJetsonView('overview');" in PAGE
  assert 'section.hidden=!section.dataset.jetsonSection.split' in PAGE
  assert 'aria-selected' in PAGE


def test_summary_shows_only_six_essential_statuses_and_details_stay_accessible():
  assert 'id="jetsonStatusGrid"' in PAGE
  assert 'data-jetson-section="overview connection"' in PAGE
  assert PAGE.count('data-summary="true"') == 6
  assert '#jetsonStatusGrid>.card:not([data-summary])' in PAGE
  for item in ('physicalUsb', 'hudPhysical', 'model', 'nativeStatus', 'error', 'version'):
    assert f'id="{item}"' in PAGE
  # All underlying status fields remain available on the Connection tab.
  assert 'id="stages"' in PAGE and 'id="linkChecks"' in PAGE


def test_advanced_options_are_folded_not_deleted():
  assert '고급 · Offroad AI 엔진 시험</summary>' in PAGE
  assert '고급 · 젯슨 LTE 인터넷 공유</summary>' in PAGE
  assert '고급 · SSH 접속 설정</summary>' in PAGE
  assert '진단에 포함되는 자료와 주의 사항</summary>' in PAGE
  for action in ('bundleDownload', 'bundleTrialStart', 'hudUpdateInstall',
                 'experimentStart', 'parkedAiCheck', 'lteEnable', 'saveTarget'):
    assert PAGE.count(f'id="{action}"') == 1


def test_no_accidental_change_to_update_or_engine_safety_checks():
  assert 'hudUpdateInstall' in PAGE
  assert "['completed','failed'].includes(j.stage)" in PAGE
  assert "j.stage==='unknown'" in PAGE
  assert 'bundleTrialStart' in PAGE and 'experimentStart' in PAGE
  assert "if(destructive&&!confirm(" in PAGE
  # Each panel is only shown or hidden; no auto-trigger for experiments, installers.
  assert "button.addEventListener('click',()=>setJetsonView(button.dataset.jetsonTab));" in PAGE
  assert PAGE.count('<details') == PAGE.count('</details>')


def test_menu_is_a_left_sidebar_with_main_content():
  assert 'class="jetson-layout"' in PAGE
  assert 'class="jetson-tabs" aria-label="젯슨 관리 메뉴"' in PAGE
  assert 'class="jetson-content"' in PAGE
  assert '.jetson-layout{display:grid;grid-template-columns:148px minmax(0,1fr)' in PAGE
  assert '.jetson-tabs{position:sticky;top:0;' in PAGE
  assert 'if(document.body&&document.body.dataset)' in PAGE
  assert '<nav class="jetson-tabs"' in PAGE
  assert PAGE.index('<nav class="jetson-tabs"') < PAGE.index('<div class="jetson-content">')


def test_update_screen_is_a_compact_live_summary_and_keeps_safety_gate():
  for summary in ('hudSummaryRelease', 'hudSummaryInstalled', 'hudSummaryAvailable', 'hudSummarySafety'):
    assert PAGE.count(f'id="{summary}"') == 1
    assert f"$('{summary}').textContent" in PAGE
  assert '<summary>상세 상태 및 설치 이력 보기</summary>' in PAGE
  assert PAGE.count('id="hudUpdateState"') == 1
  assert PAGE.count('id="hudUpdateJob"') == 1
  assert 'id="hudUpdateMessage"' in PAGE
  assert "d.can_install_safely===true&&!activeJob" in PAGE
  assert "hudUpdateCall('/api/jetson/hud-update/start',{confirm:true,commit:hudUpdateCommit})" in PAGE


def test_sidebar_touches_viewport_left_and_7000_style_has_no_centered_page():
  assert 'body.jetson-management main{width:100%;max-width:none;min-height:100vh;margin:0;padding:0}' in PAGE
  assert '<body class="jetson-management"><main>' in PAGE
  assert 'class="jetson-menu-caption">JETSON 관리</div>' in PAGE
  assert 'border:0;border-right:1px solid #324151;border-radius:0' in PAGE
  assert 'button[aria-selected=true]{background:#58483a;border-left-color:#eba15e' in PAGE
  assert '@media(max-width:560px)' in PAGE
  # The Carrot-like rail precedes the content. Page header is inside the content,
  # not above the sidebar where it would displace the left rail.
  assert PAGE.index('<div class="jetson-layout">') < PAGE.index('<nav class="jetson-tabs"')
  assert PAGE.index('<nav class="jetson-tabs"') < PAGE.index('<div class="jetson-content">')
  assert PAGE.index('<div class="jetson-content">') < PAGE.index('<header><h1>NVIDIA Jetson</h1>')
