"""Jetson runtime and installation screens; no device control in the guide."""

STYLE = '''<style>
[hidden]{display:none!important}:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif}*{box-sizing:border-box}
body{margin:0;background:#0b1016;color:#eef2f6}main{max-width:960px;margin:auto;padding:24px 16px 48px}
header,.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}header{justify-content:space-between;margin-bottom:20px}
h1{font-size:25px;margin:0}h2{font-size:18px;margin:0 0 12px}a,button{color:#d7e3f0;background:#1c2937;border:1px solid #46515d;
border-radius:10px;padding:10px 14px;text-decoration:none;font:inherit;cursor:pointer}button:disabled{opacity:.4;cursor:default}
.hero,.card{border:1px solid #303b47;border-radius:16px;padding:18px;background:#141b23;margin-bottom:12px}
.hero{border-left:5px solid #687785}.hero.online{border-left-color:#55d58b}.hero.waiting{border-left-color:#e4b55f}.hero.error{border-left-color:#ff7c73}
.state{font-size:22px;font-weight:800}.sub,.label{color:#a3b2c1;font-size:14px;line-height:1.6}.sub{margin-top:8px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.grid .card{margin:0;min-width:0}
.stages{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}
.stage{border:1px solid #46515d;border-radius:10px;padding:12px;min-width:0;overflow-wrap:anywhere}
.stage[data-state=OK]{border-color:#55d58b}.stage[data-state=WAITING]{border-color:#e4b55f}.stage[data-state=ERROR]{border-color:#ff7c73}.stage strong{display:block;margin-top:6px}
.value{font-size:17px;font-weight:700;overflow-wrap:anywhere;margin-top:5px}.section{margin-top:20px}
input{background:#0b1016;border:1px solid #46515d;border-radius:8px;padding:10px;color:#eef2f6;font:inherit;width:min(420px,100%)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font-size:13px;line-height:1.6}
li{margin:12px 0;line-height:1.7}strong{color:#8ae0b0}button.danger{border-color:#ab6b64}
@media(max-width:650px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.stages{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:390px){.grid{grid-template-columns:1fr}}

/* Keep the original 7000 semantics: compact views, unchanged controls and safety gates. */
.jetson-tab-hint{margin:5px 0 12px}
body:not([data-jetson-view]) [data-jetson-section]:not([data-jetson-section~="overview"]){display:none}
body:not([data-jetson-view]) #jetsonStatusGrid>.card:not([data-summary]),
body[data-jetson-view="overview"] #jetsonStatusGrid>.card:not([data-summary]){display:none}
details.compact-extra{border:1px solid #384553;border-radius:10px;padding:9px 12px;margin:8px 0 12px}
details.compact-extra[open]{background:#101923}
details.card>summary,details.compact-extra>summary{cursor:pointer;color:#d7e3f0;font-weight:700}
details.card>summary{font-size:16px}
details.card[open]>summary{margin-bottom:12px}
details.card label{display:block;margin:10px 0}

/* Carrot 7000-like sidebar flush against the browser's left edge. */
main{width:100%;max-width:none;min-height:100vh;margin:0;padding:0}
.jetson-layout{display:grid;grid-template-columns:148px minmax(0,1fr);gap:0;align-items:start;width:100%;min-height:100vh}
.jetson-content{min-width:0;padding:15px 18px 34px}
.jetson-content>header{margin-bottom:14px}
.jetson-tabs{position:sticky;top:0;z-index:10;display:flex;flex-direction:column;gap:0;
  box-sizing:border-box;width:100%;min-height:100vh;margin:0;padding:8px 0;
  border:0;border-right:1px solid #324151;border-radius:0;background:#152030}
.jetson-menu-caption{font-size:11px;font-weight:750;letter-spacing:.09em;color:#8496a9;padding:15px 13px 12px}
.jetson-tabs button{min-width:0;width:100%;margin:0;min-height:54px;padding:14px 15px;
  text-align:left;line-height:1.3;font-size:14px;font-weight:650;white-space:normal;
  background:transparent;border:0;border-left:3px solid transparent;border-radius:0;color:#c6d3e0}
.jetson-tabs button:hover{background:#223246;color:#f6f9fc}
.jetson-tabs button:focus-visible{outline:2px solid #a7c8e7;outline-offset:-3px}
.jetson-tabs button[aria-selected=true]{background:#58483a;border-left-color:#eba15e;color:#ffdfb8}
.jetson-update-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin:12px 0}
.jetson-update-summary>.update-tile{min-width:0;padding:12px;border-radius:10px;background:#1a2837;border:1px solid #354859}
.jetson-update-summary .value{font-size:15px}
.jetson-update-actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.jetson-update-actions button{flex:1;min-width:155px}
#hudUpdateMessage{margin-top:10px;overflow-wrap:anywhere}
@media(max-width:820px){
  .jetson-layout{grid-template-columns:116px minmax(0,1fr)}
  .jetson-content{padding:12px 10px 26px}
  .jetson-tabs button{min-height:49px;font-size:12px;padding:11px 9px}
  .jetson-menu-caption{padding:14px 9px 9px}
}
@media(max-width:560px){
  .jetson-layout{grid-template-columns:83px minmax(0,1fr)}
  .jetson-tabs button{min-height:45px;font-size:11px;padding:10px 5px}
  .jetson-menu-caption{padding:12px 5px 8px;font-size:10px}
  .jetson-content{padding:10px 7px 22px}
  .jetson-content .hero,.jetson-content .card{padding:11px}
  .jetson-update-summary,.jetson-content .grid{grid-template-columns:1fr}
  .jetson-content .stages{grid-template-columns:repeat(2,minmax(0,1fr))}
  .jetson-update-actions button{min-width:0;width:100%}
}
</style>'''

PAGE = '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jetson 관리 · XPlus</title>''' + STYLE + '''</head><body><main>
<div class="jetson-layout">
<nav class="jetson-tabs" aria-label="젯슨 관리 메뉴">
<div class="jetson-menu-caption">JETSON 관리</div>
<button type="button" data-jetson-tab="overview" aria-selected="true">요약</button>
<button type="button" data-jetson-tab="update" aria-selected="false">업데이트</button>
<button type="button" data-jetson-tab="ai" aria-selected="false">AI·HUD</button>
<button type="button" data-jetson-tab="diagnostics" aria-selected="false">진단</button>
<button type="button" data-jetson-tab="connection" aria-selected="false">연결·설정</button>
</nav><div class="jetson-content">
<header><h1>NVIDIA Jetson</h1><div class="actions"><a href="/jetson/install">Jetson 설치 가이드</a><a href="/">7000 홈</a></div></header>
<section id="hero" class="hero"><div id="stateText" class="state">연결 확인 중…</div>
<div id="diagnosis" class="sub">Jetson 상태를 기다리고 있습니다.</div></section>
<p id="jetsonTabHint" class="sub jetson-tab-hint">핵심 연결 상태만 간단히 보여드립니다.</p>
<section class="card section" data-jetson-section="overview"><h2>Jetson → USB → Jetlink → Camera → Inference → HUD</h2>
<div id="stages" class="stages" aria-live="polite">단계별 상태 확인 중</div>
<p id="recentStages" class="sub">최근 heartbeat · 영상 · 추론 확인 중</p>
<p id="negotiation" class="sub">기능 협상 확인 중</p></section>
<section class="card section" data-jetson-section="connection"><h2>USB · IP · Jetlink 상세 진단 (읽기 전용)</h2>
<div id="linkSummary" class="value" aria-live="polite">진단 수집 중</div>
<pre id="linkChecks">USB 네트워크 점검 중…</pre>
<p class="sub">콤마에서 읽기 전용으로 확인합니다. 실제 젯슨 수신과 HUD 패널 출력은 별도 검증이 필요합니다.</p></section>
<section id="jetsonStatusGrid" class="grid" data-jetson-section="overview connection">
<div class="card" data-summary="true"><div class="label">USB 물리 연결</div><div id="physicalUsb" class="value">확인 중</div></div>
<div class="card"><div class="label">HUD TCP 서버 (5599)</div><div id="hudServer" class="value">확인 중</div></div>
<div class="card" data-summary="true"><div class="label">외부 HUD 실제 화면</div><div id="hudPhysical" class="value">미검증</div></div>
<div class="card"><div class="label">Jetson IP</div><div id="ip" class="value">확인 중</div></div>
<div class="card"><div class="label">comma4 연결</div><div id="comma" class="value">확인 중</div></div>
<div class="card"><div class="label">USB 연결</div><div id="usb" class="value">확인 중</div></div>
<div class="card"><div class="label">USB3 / 실제 협상 속도</div><div id="usb3" class="value">확인 중</div></div>
<div class="card"><div class="label">서비스 실행</div><div id="service" class="value">확인 중</div></div>
<div class="card"><div class="label">영상 / 최근 프레임</div><div id="frame" class="value">확인 중</div></div>
<div class="card" data-summary="true"><div class="label">YOLO / AI 모델 실행</div><div id="model" class="value">확인 중</div></div>
<div class="card" data-summary="true"><div class="label">NEXO Jetlink 모델</div><div id="nativeStatus" class="value">확인 중</div></div>
<div class="card"><div class="label">모델 준비</div><div id="ready" class="value">확인 중</div></div>
<div class="card"><div class="label">마지막 통신</div><div id="age" class="value">확인 중</div></div>
<div class="card" data-summary="true"><div class="label">오류</div><div id="error" class="value">확인 중</div></div>
<div class="card"><div class="label">연결 경로 / 모델</div><div id="pipeline" class="value">확인 중</div></div>
<div class="card"><div class="label">5600 웹 관리</div><div id="web" class="value">확인 중</div></div>
<div class="card"><div class="label">Jetson 온도 / thermal</div><div id="temperature" class="value">확인 중</div></div>
<div class="card"><div class="label">연결방식 / 인터페이스</div><div id="interface" class="value">확인 중</div></div>
<div class="card"><div class="label">저장장치 상태</div><div id="storage" class="value">확인 중</div></div>
<div class="card" data-summary="true"><div class="label">Jetson 버전</div><div id="version" class="value">확인 중</div></div>
</section>
<section class="card section" data-jetson-section="ai"><h2>AI · GPU 실측 (읽기 전용)</h2>
<div id="aiLiveState" class="value" aria-live="polite">실행 증거 확인 중</div>
<pre id="aiLiveMetrics">측정 대기</pre>
<p class="sub">추론 FPS는 완료 프레임 수로 계산합니다. 왕복시간에는 통신·준비·실행이 포함됩니다.
GPU 부하는 장치 전체 값이며 AI 실행의 증거가 아닙니다. Offroad 합성 테스트는 실차 검증과 별개입니다.</p></section>
<section class="card section" data-jetson-section="diagnostics"><h2>메모리 · 복구 진단</h2><pre id="stability">진단 대기</pre>
<p class="sub">5초 간격 RSS 관측 · 최대 5분 증가량입니다. 실차 장시간 안정성 검증 결과가 아닙니다. USB 전원은 읽기 전용 진단입니다.</p></section>
<section class="card section" data-jetson-section="diagnostics"><h2>Preview 전송 관찰</h2><p id="previewMetrics" class="sub">최근 preview 측정값 대기</p>
<p class="sub">Camera TX는 송신 완료입니다. Jetson의 수신 확인은 “영상 수신 테스트”에서 별도로 확인합니다.
carrot 연결은 작은 road preview부터 시작하며, 측정값이 없으면 정상으로 추정하지 않습니다.</p></section>
<section class="card section" data-jetson-section="connection"><h2>연결과 상태 확인</h2><div class="actions">
<button id="refresh">상태 새로고침</button><button data-action="status">Jetson 연결 확인</button>
<button data-action="diagnose">통신 진단</button><button data-action="video_test">영상 수신 테스트</button>
<button data-action="model">모델 상태 · 준비 확인</button><a id="webOpen" hidden target="_blank" rel="noopener noreferrer">Jetlink 열기</a>
</div><pre id="checkResult" aria-live="polite" hidden></pre>
<div id="checkDownloads" class="actions" hidden><button id="checkTxt">결과 TXT 저장</button>
<button id="checkJson">결과 JSON 저장</button></div><p class="sub">기본 carrot Windows 이미지에는 5600 웹 관리가 없을 수 있습니다. USB 상태와 별도로 확인합니다.
영상이 없는 offroad 상태에서는 영상 수신과 추론을 정상으로 추정하지 않습니다.</p></section>
<section class="card section" data-jetson-section="connection"><h2>자동 USB 인식 · 젯슨 부팅/서비스 · 복구 단계</h2>
<p class="sub">Jetlink 추론이 꺼져 있어도 6초 간격으로 NCM 장치와 링크를 관측합니다.
원격 신호가 없을 때는 젯슨 전원이나 서비스 고장으로 단정하지 않습니다.</p>
<pre id="autoObserver" aria-live="polite">자동 관측 준비 중…</pre></section>
<section class="card section" data-jetson-section="diagnostics"><h2>젯슨 통합진단 · AI 원인 분석 포함</h2>
<details class="compact-extra"><summary>진단에 포함되는 자료와 주의 사항</summary><p class="sub">USB 연결과 AI 추론 왕복시간, 젯슨 CPU·GPU 클럭·전력 모드·스레드 대기시간·메모리·HUD Python GC·영상·패널 깜박임 이력을 함께 모읍니다.
AI 집중 진단(10_ai_root_cause.txt/json)에서 콤마4·젯슨 모델 SHA 일치 여부, 현재 모델의 TensorRT Conv/GELU 실패 분류,
GPU 엔진 생성·실험 기록, 당근 빌더 지문과 L4T·TensorRT 설치 정보를 구분해 확인합니다.
SSH에 연결되지 않아도 콤마4에서 확인 가능한 결과를 저장하며, 확인하지 못한 항목은 미검증으로 표시합니다.
일반 통합진단 ZIP은 읽기 전용이며 엔진 생성·AI 활성화·주행 제어·젯슨 서비스를 변경하지 않습니다.
자세한 젯슨 내부 값은 SSH 연결이 필요합니다.</p>
<p class="sub">추가 자료: <strong>11_offroad_ai_engine_trial.txt/json</strong>에는 ZIP 저장 시점의 완전 Offroad 확인,
기존 독립 엔진 시험의 실행 여부·실패 단계·합성 GPU 결과를 기록합니다.
실험이 끝난 뒤 ZIP을 다시 내려받으시면 최신 시험 결과가 포함됩니다.</p></details>
<details class="compact-extra"><summary>고급 · 완전 Offroad AI 엔진 시험</summary>
<div class="actions"><button id="bundleTrialStart" disabled>완전 Offroad AI 엔진 시험 시작 (native_fp16)</button>
<button id="bundleTrialStatus">AI 엔진 시험 진행 상태 확인</button></div>
<pre id="bundleTrialResult" aria-live="polite">시험은 자동 실행되지 않습니다. 시동 OFF·완전 Offroad에서만 명시적으로 시작할 수 있습니다.
SSH 신뢰 등록과 인증이 미완료면 시험을 시작하지 않습니다.</pre></details>
<div class="actions"><button id="bundleDownload">젯슨 통합진단 ZIP 다운로드 (시험 이력 포함)</button></div>
<p id="bundleResult" class="sub" role="status">일반 통합진단 버튼은 시험을 실행하지 않고 현재 저장된 시험 이력만 ZIP에 포함합니다.</p></section>
<section class="card section" data-jetson-section="diagnostics"><h2>연결 이력 · 상태 변화 최대 80건</h2>
<p class="sub">2회 연속 관측한 상태 변화만 기록합니다. 기록을 위해 추론을 켜거나 USB 장치를 재설정하지 않습니다.</p>
<div class="actions"><button id="refreshHistory">이력 새로고침</button>
<a href="/api/jetson/history?format=txt" download>이력 TXT 다운로드</a>
<a href="/api/jetson/history?format=json" download>이력 JSON 다운로드</a></div>
<pre id="linkHistory">연결 이력 확인 중…</pre></section>
<section class="card section" data-jetson-section="diagnostics"><h2>USB NCM · 연결 원인 상세 진단</h2>
<p class="sub">차 안에서 SSH 명령어를 입력하지 않아도 콤마4의 USB Gadget, UDC, NCM IP와 전용 경로를 읽기 전용으로 확인합니다. 젯슨에 응답이 없을 때 전원·케이블 고장을 단정하지 않습니다.</p>
<div class="actions"><button id="localDiagBtn">USB·젯슨 진단 실행</button>
<a href="/api/jetson/diagnostics?format=txt" download>진단 TXT 저장</a>
<a href="/api/jetson/diagnostics?format=json" download>진단 JSON 저장</a></div>
<pre id="localDiag" aria-live="polite">버튼을 눌러 연결 원인을 확인하십시오.</pre></section>
<section class="card section" data-jetson-section="ai"><h2>주변 차량 등화 · 전방 영상 실험 기능</h2>
<div id="lampOptions"><label><input type="checkbox" value="enabled"> 주변 차량 등화</label>
<label><input type="checkbox" value="brake"> 브레이크등</label>
<label><input type="checkbox" value="turn"> 방향지시등</label>
<label><input type="checkbox" value="hazard"> 비상등</label></div>
<button id="saveLamps" disabled>표시 설정 저장</button><span id="lampResult" role="status"></span></section>
<section class="card section" data-jetson-section="update"><h2>엑스플러스 HUD 자동 업데이트</h2>
<p class="sub">검증된 HUD 패키지만 설치합니다. 시동 OFF·Offroad 또는 P단·완전 정지·주행 제어 비활성 상태가 확인되어야 합니다.</p>
<div class="jetson-update-summary" aria-live="polite">
<div class="update-tile"><div class="label">최신 검증 패키지</div><div id="hudSummaryRelease" class="value">확인 전</div></div>
<div class="update-tile"><div class="label">젯슨 현재 버전</div><div id="hudSummaryInstalled" class="value">확인 전</div></div>
<div class="update-tile"><div class="label">업데이트 여부</div><div id="hudSummaryAvailable" class="value">확인 전</div></div>
<div class="update-tile"><div class="label">SSH · 차량 안전</div><div id="hudSummarySafety" class="value">확인 전</div></div>
</div>
<div class="jetson-update-actions"><button id="hudUpdateCheck">최신 HUD 버전 확인</button>
<button class="danger" id="hudUpdateInstall" disabled>젯슨 HUD 업데이트 설치</button></div>
<p id="hudUpdateMessage" class="sub" role="status">최신 버전 확인을 누르면 설치 가능 여부를 점검합니다.</p>
<details class="compact-extra"><summary>상세 상태 및 설치 이력 보기</summary>
<pre id="hudUpdateState" aria-live="polite">버전을 확인해 주세요.</pre>
<pre id="hudUpdateJob" aria-live="polite">진행 중인 설치 없음</pre>
<p class="sub">설치 중에는 P단과 젯슨 전원을 유지해 주세요. 실제 외부 HUD 화면은 설치 후 별도 확인이 필요합니다.</p></details></section>
<details class="card section" data-jetson-section="update"><summary>고급 · 젯슨 서비스 관리 및 로그</summary><div class="actions">
<button data-action="logs">Jetson 로그 확인</button><button data-action="check_update">Jetson 업데이트 확인</button>
<button data-action="settings">Jetson 설정 확인</button><button class="danger" id="restart" data-action="restart" disabled>서비스 재시작</button>
<button class="danger" id="reconnect" data-action="reconnect" disabled>Jetson 재연결</button>
</div><p id="restartHint" class="sub">재시작은 정차 상태와 모델 비활성이 확인될 때 사용할 수 있습니다.</p></details>
<details class="card section" data-jetson-section="ai"><summary>고급 · Offroad AI 엔진 시험</summary>
<p>시동을 끄고 외부 AI를 끈 상태에서 현재 모델로 독립 엔진을 빌드합니다. 합성 입력 결과와 실패한 단계를 확인할 수 있습니다.</p>
<label>젯슨에서 콤마에 접속할 SSH 계정 <input id="experimentUser" value="comma" maxlength="32"></label>
<label>젯슨의 기존 승인 개인키 경로 (선택 · 비우면 기존 SSH 설정/agent 사용)
<input id="experimentIdentity" placeholder="예: /home/jetlink/.ssh/id_ed25519" maxlength="240"></label>
<button id="experimentSsh">젯슨 → 콤마 SSH 확인</button>
<pre id="experimentSshResult"></pre>
<p class="sub">SSH_HOST_KEY_UNKNOWN 해결: 아래 버튼은 USB로 보이는 공개키 <strong>후보</strong>만 보여줍니다.
콤마4 본체의 SSH 서버 공개키 지문을 별도 터미널에서 직접 확인하고 일치할 때만 등록하세요.
후보 지문을 확인 없이 그대로 입력하면 안전하지 않습니다. 등록해도 SSH 로그인용 개인키/승인 공개키는 별도로 필요합니다.</p>
<div class="actions"><button id="experimentHostPreview">콤마4 호스트키 후보 확인 (읽기 전용)</button></div>
<label>콤마4 본체에서 별도로 검증한 SHA256 호스트키 지문
<input id="experimentHostFingerprint" placeholder="SHA256:..." maxlength="80" autocomplete="off"></label>
<div class="actions"><button class="danger" id="experimentHostEnroll">일치하는 호스트키만 등록 (Offroad)</button></div>
<pre id="experimentHostResult" aria-live="polite">기존 키·known_hosts는 자동 삭제하지 않습니다.</pre>
<label>시험 선택 <select id="experimentTrial"><option value="">전체 비교</option><!-- engine-trials --></select></label>
<div class="actions"><button id="experimentStart" disabled>Offroad 엔진 시험 시작</button>
<button id="experimentStatus">진행 상태·결과 확인</button></div>
<pre id="experimentResult"></pre>
<div class="actions"><button id="parkedAiCheck" disabled>P단 AI 안전 진단 (읽기 전용)</button></div>
<p class="sub">시동 ON 상태에서도 실제 P단·완전 정지·주행 제어 비활성을 실시간 확인하여
AI 모델 일치·기존 엔진·이전 빌드 오류만 조회합니다. 젯슨 → 콤마 역방향 SSH는 필요하지 않습니다.
이 버튼은 TensorRT 엔진 생성이나 GPU 합성 추론을 실행하지 않습니다.</p>
<pre id="parkedAiResult" aria-live="polite">P단에서 안전하게 정차한 후 진단 버튼을 누르세요.</pre></details>
<details class="card section" data-jetson-section="connection"><summary>고급 · 젯슨 LTE 인터넷 공유</summary>
<p class="sub">콤마4 유심 인터넷을 기존 USB-NCM으로 젯슨에 전달합니다.
기존 HUD·AI 연결과 Wi-Fi는 변경하지 않습니다. 완전 Offroad에서만 켜거나 끌 수 있으며
휴대전화 핫스팟을 사용하지 않는 상태에서 실제 인터넷·DNS 확인이 필요합니다.
기본값은 비활성이고 재부팅 시 자동으로 켜지지 않습니다.</p>
<div class="actions"><button id="lteStatus">LTE 공유 상태 확인</button>
<button class="danger" id="lteEnable" disabled>공유 켜기</button>
<button id="lteDisable" disabled>공유 끄기</button></div>
<button id="lteProbe" disabled>USB 인터넷·DNS 검사</button>
<pre id="lteResult" aria-live="polite">상태 확인을 누르면 Offroad 여부와 공유 변경 가능 상태를 표시합니다.</pre></details>
<details class="card section" data-jetson-section="connection"><summary>고급 · SSH 접속 설정</summary>
<p class="sub">키 기반 SSH가 필요합니다. Jetson SSH 공개키와 known_hosts를 먼저 등록하고 기존 SSH 별칭 또는 사용자@호스트를 입력하세요.
계정과 IP를 자동 추정하지 않습니다. SSH 대상은 /etc/nv_tegra_release 확인을 통과해야 합니다.</p>
<label for="sshTarget" class="label">SSH 대상</label><div class="actions">
<input id="sshTarget" placeholder="SSH 별칭 또는 사용자@호스트" maxlength="128" autocomplete="off">
<button id="saveTarget">SSH 대상 저장</button></div></details>
<section class="card section" data-jetson-section="update connection"><h2>확인 결과</h2><pre id="result" aria-live="polite">버튼을 누르면 확인 결과가 표시됩니다.</pre></section>
</div></div></main><script>
const $=id=>document.getElementById(id);
const jetsonViews={
  overview:'핵심 연결 상태만 간단히 보여드립니다.',
  update:'HUD 업데이트와 젯슨 서비스 관리',
  ai:'AI 실측과 외부 HUD 표시 · 고급 엔진 시험은 접어서 보관',
  diagnostics:'통합진단 ZIP과 원인 분석 및 이력',
  connection:'USB 연결과 네트워크 · 고급 설정'
};
function setJetsonView(requested){
  const view=Object.prototype.hasOwnProperty.call(jetsonViews,requested)?requested:'overview';
  // Browserless JavaScript regression harnesses may omit document.body.
  if(document.body&&document.body.dataset)document.body.dataset.jetsonView=view;
  document.querySelectorAll('[data-jetson-section]').forEach(section=>{
    section.hidden=!section.dataset.jetsonSection.split(' ').includes(view);
  });
  document.querySelectorAll('[data-jetson-tab]').forEach(button=>{
    button.setAttribute('aria-selected',String(button.dataset.jetsonTab===view));
  });
  $('jetsonTabHint').textContent=jetsonViews[view];
}
document.querySelectorAll('[data-jetson-tab]').forEach(button=>{
  button.addEventListener('click',()=>setJetsonView(button.dataset.jetsonTab));
});
setJetsonView('overview');
const stateLabel=(v,connected)=>!connected?'미연결':v===true?'정상':v===false?'대기':'확인 불가';
const ageLabel=v=>Number.isFinite(v)?v.toFixed(2)+'초 전':'확인 불가';
const stageLabel={OK:'정상',WAITING:'대기',DISCONNECTED:'끊김',ERROR:'오류',OFF:'비활성'};
let token='',busy=false,refreshing=false,configured=false, bundleTrialBusy=false, bundleTrialPoll=null;
const lamps=[...document.querySelectorAll('#lampOptions input')];
async function lampOptions(save=false){
  $('saveLamps').disabled=true;
  try{
    const opts=save?{method:'POST',headers:{'Content-Type':'application/json','X-Jetson-Token':token},
      body:JSON.stringify(Object.fromEntries(lamps.map(el=>[el.value,el.checked])))}:{};
    const response=await fetch('/api/jetson/vehicle-lamps',{...opts,cache:'no-store'});
    if(!response.ok)throw Error('HTTP '+response.status);
    const values=await response.json();
    lamps.forEach(el=>{el.checked=values[el.value]===true;});
    $('lampResult').textContent=save?'저장됨':'';
  }catch(e){$('lampResult').textContent=String(e);}
  finally{$('saveLamps').disabled=false;}
}
$('saveLamps').onclick=()=>lampOptions(true);
lampOptions();
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    const response=await fetch('/api/jetson/status',{cache:'no-store'});
    if(!response.ok)throw new Error('상태 API 응답 '+response.status);
    const d=await response.json(),s=d.status||{},jl=d.jetlink||{},local=d.local_jetlink||{},live=d.connected===true;
    const waiting=local.waiting_modeld===true,enabledWaiting=local.enabled===true&&!live;
    const display=d.display_observation||{},detected=d.auto_detection||{},recovery=d.network_recovery||{},
          assessment=d.observer_summary||{},remote=d.remote_boot_and_services||{},ssh=d.remote_service_check||{};
    const linkDiag=d.link_diagnostics||{};
    $('linkSummary').textContent=linkDiag.summary||'상세 진단 결과 없음';
    $('linkChecks').textContent=(linkDiag.checks||[]).map(c=>
      '['+(c.state||'UNKNOWN')+'] '+(c.label||'')+'\\n'+(c.detail||'')+
      (c.advice&&c.state!=='OK'&&c.state!=='OFF'?'\\n확인: '+c.advice:'')
    ).join('\\n\\n')+'\\n\\n'+(linkDiag.notes||[]).join('\\n');
    const fmt=v=>v===true?'예':v===false?'아니요':v==null?'확인 불가':String(v);
    $('autoObserver').textContent=[
      '자동 진단: '+(assessment.code||'관측 대기'),
      '원인: '+(assessment.reason||d.observer_error||'관측 대기'),
      'g1 NCM: '+(detected.ncm_interface||'확인 불가')+' · 소유자 '+(detected.gadget_owners||[]).join(', '),
      'NCM carrier: '+fmt(detected.ncm_carrier??local.usb_carrier)+' · 콤마 NCM IP: '+(detected.comma_ipv4??'확인 불가'),
      '젯슨 USB 전용 경로: '+fmt(detected.peer_route_via_usb),
      '젯슨 부팅 완료: 확인 불가 (비콘만으로 부팅 완료를 판단하지 않음)',
      '최근 젯슨 앱 상태: '+fmt(remote.reported_app_active),
      '원격 서비스: '+(ssh.fresh?Object.entries(ssh.services||{}).map(([k,v])=>k+'='+v).join(', ')+' ('+ssh.age_s+'초 전)':'확인 불가 · 위 Jetson 연결 확인 버튼에서 SSH 상태 조회'),
      'USB NCM 복구: '+(recovery.state||'확인 불가')+' · 재시도 '+fmt(recovery.attempts)+' · 복구 '+fmt(recovery.recoveries),
      '권장 조치: '+(assessment.recommended_action||'상세 진단을 실행하십시오.'),
      d.observer_error||''
    ].filter(Boolean).join('\\n');
    $('physicalUsb').textContent=local.usb_carrier===true&&local.usb_admin_up===true?'USB 링크 연결됨 · Jetson 앱 응답과 별개':'USB 링크 미확인';
    $('hudServer').textContent=display.server_reachable===true?'TCP 응답 · 화면 출력 별도 확인':display.detail||'미응답 또는 비활성';
    $('hudPhysical').textContent=display.hud_output_verified===true?'검증됨':'미검증 · Jetson USB 인식/실제 화면 확인 필요';
    token=d.csrf||'';
    $('experimentStart').disabled=!(token && local.offroad===true && local.onroad===false && local.enabled===false) || experimentBusy;
    $('bundleTrialStart').disabled=!(token && local.offroad===true && local.onroad===false && local.enabled===false) || bundleTrialBusy;
    $('parkedAiCheck').disabled=!token || parkedAiBusy;
    $('hero').className='hero '+(d.state==='오류'?'error':live?'online':enabledWaiting?'waiting':'');
    $('stateText').textContent=live?d.state+' · Jetson 연결됨':waiting?'대기 · Jetlink 활성화됨':enabledWaiting?d.state+' · Jetlink 연결 대기':d.state+' · Jetson 연결 확인';
    $('diagnosis').textContent=d.diagnosis+(d.native_link?.wait_reason?' · '+d.native_link.wait_reason:'')
      +(d.receiver_error?' · '+d.receiver_error:'');
    $('stages').replaceChildren();
    for(const stage of d.stages||[]){
      const box=document.createElement('div'),label=document.createElement('div'),state=document.createElement('strong'),detail=document.createElement('div');
      box.className='stage';box.dataset.state=stage.state;label.textContent=stage.label;
      state.textContent=stageLabel[stage.state]||'확인 불가';detail.className='sub';detail.textContent=stage.detail||'';
      box.append(label,state,detail);$('stages').append(box);
    }
    const recent=d.recent||{},health=d.health||{},p=d.preview||{};
    const stability=d.stability||{},memoryLines=[];
    for(const [key,label] of [['comma_memory','comma'],['jetson_memory','Jetson']]){
      const m=stability[key]||{};
      memoryLines.push(label+': '+(m.available===true&&Number.isFinite(m.used_bytes)?
        (m.used_bytes/1048576).toFixed(1)+' MiB · 증가 '+((m.growth_bytes||0)/1048576).toFixed(1)+' MiB / '+(m.window_s||0).toFixed(0)+'초':'확인 불가'));
      for(const proc of m.processes||[])memoryLines.push('  '+proc.role+' PID '+proc.pid+' RSS '+((proc.rss_bytes||0)/1048576).toFixed(1)+' MiB');
    }
    memoryLines.push('HUD 복구: '+JSON.stringify(stability.hud_recovery||{}),
      '영상 재구독: '+(stability.video_reconnects??'확인 불가')+' · '+(stability.video_last_error||''),
      'YOLO 서비스: '+JSON.stringify(stability.yolo_service||{}),'USB 전원: 설정 변경 없음 · 하드웨어 호환성 미확인');
    $('stability').textContent=memoryLines.join('\\n');
    $('recentStages').textContent=waiting?'오프로드 대기 · modeld 시작 후 heartbeat · 영상 · 추론 · HUD 확인':
      'Heartbeat '+ageLabel(recent.heartbeat_age_s)+' · 영상 '+ageLabel(recent.camera_frame_age_s)
      +' · 추론 '+ageLabel(recent.inference_age_s)+' · HUD TX '+ageLabel(recent.hud_tx_age_s);
    $('recentStages').textContent+=' · 비콘 USB '+ageLabel(d.beacons?.usb?.age_s)+' / Wi-Fi '+ageLabel(d.beacons?.wifi?.age_s);
    $('nativeStatus').textContent=local.enabled===false?'추론 비활성 · 로컬 모델 사용':d.native_link?.label||(waiting?'modeld 시작 대기':'최근 NEXO 모델 상태 없음');
    $('negotiation').textContent=waiting?'Jetlink 활성화됨 · modeld 시작 시 기능 협상 시작':
      d.capability_negotiated?'Jetlink 기능 협상 완료':'Jetlink 기능 협상 대기 또는 legacy 경로';
    $('temperature').textContent=(Number.isFinite(health.temperature_c)?health.temperature_c.toFixed(1)+'℃':'확인 불가')+' · '+(health.thermal||'unknown');
    $('interface').textContent=local.enabled===true?'USB · '+(local.usb_interface||'탐색 중')
      +' admin '+(local.usb_admin_state||'unknown')+' · link '+(local.usb_operstate||'unknown'):
      (health.transport||'unknown')+' · '+(health.interfaces||[]).map(i=>i.interface).filter(Boolean).join(', ');
    $('storage').textContent=health.storage==='unknown'?'확인 불가':health.storage;
    $('version').textContent=health.version==='unknown'?'확인 불가':health.version;
    const metric=(key,suffix)=>Number.isFinite(p[key])?p[key].toFixed(1)+suffix:'확인 불가';
    $('previewMetrics').textContent=waiting?'오프로드 대기 · modeld 시작 후 preview 측정 시작':
      '생성 '+metric('fps',' FPS')+' · preview CPU '+metric('cpu_percent','%')
      +' · 생성 지연 '+metric('preview_latency_ms','ms')+' · 송신 지연 '+metric('camera_tx_latency_ms','ms')
      +' · HUD 전송 '+metric('hud_send_ms','ms');
    if(waiting){
      $('ip').textContent='응답 대기';
      $('comma').textContent='Jetson 응답 대기';
      $('usb3').textContent=local.usb_carrier===true?'협상 대기':'확인 대기';
      $('service').textContent='활성화됨 · daemon 시작 대기';
      $('frame').textContent='modeld 시작 후 확인';
      $('model').textContent='modeld 시작 후 확인';
      $('ready').textContent='modeld 시작 후 확인';
      $('age').textContent='수신 기록 없음 · 오프로드 대기';
      $('pipeline').textContent='NEXO Jetlink 추론 USB · NexoJetsonUsb OFF(정상)';
      $('web').textContent='Jetson 응답 후 확인';
    }else{
      $('ip').textContent=live?(s.ip||'USB 연결 · IP 확인 불가'):'미연결';
      for(const [id,key]of [['comma','comma_connected'],['usb','usb_connected'],['service','service_active'],
        ['frame','camera_frame_seen'],['model','model_active'],['ready','model_ready']])$(id).textContent=stateLabel(s[key],live);
      $('usb3').textContent=live?(s.usb3===true?'USB3 · '+s.usb_speed:s.usb3===false?'USB2 이하 · '+s.usb_speed:'확인 불가'):'미연결';
      $('age').textContent=d.age_ms==null?'수신 기록 없음':(d.age_ms/1000).toFixed(1)+'초 전'+(live?'':' · 연결 만료');
      $('pipeline').textContent=live?(s.pipeline||s.protocol||d.native_link?.model||'상태 신호'):enabledWaiting?(local.summary||'Jetlink 연결 대기'):'미연결';
      $('web').textContent=jl.reachable?'응답 중':live?'미제공 또는 응답 없음':enabledWaiting?'Jetson 응답 후 확인':'미연결';
    }
    if(d.hud_service_state)$('service').textContent='HUD · '+d.hud_service_state;
    $('frame').textContent='송신 '+(d.camera_tx_recent?'확인':'기록 없음')+' · 수신 '+(d.camera_rx_recent?'확인':'기록 없음');
    $('recentStages').textContent+=' · HUD 렌더러 '+(d.hud_pipeline?.renderer_stage||'상태 미확인')+' / PID '+(d.hud_pipeline?.renderer_pid??'미확인')+' · 패널 쓰기 '+fmt(d.hud_output_reported);
    if(d.clock_sync)$('recentStages').textContent+=' · 시각 동기화 '+[d.clock_sync.status,d.clock_sync.reason].filter(Boolean).join(' / ');
    if(enabledWaiting||(local.enabled===true&&local.usb_admin_up===false))
      $('usb').textContent=(d.stages||[]).find(stage=>stage.id==='usb')?.detail||'USB 상태 확인 대기';
    $('error').textContent=s.last_error||(local.conflict===true?(local.summary||'Jetlink 설정 충돌'):'보고된 오류 없음');
    $('webOpen').hidden=!jl.reachable;if(jl.reachable)$('webOpen').href=jl.url;
    for(const id of ['restart','reconnect'])$(id).disabled=!d.can_restart||busy||d.management_busy;
    $('restartHint').textContent=waiting?'오프로드 · modeld 시작 대기 중이라 Jetson 서비스 제어가 필요하지 않습니다.':
      d.can_restart?'정차 확인됨 · 재시작 전 확인 창이 표시됩니다.':'주행 중 또는 상태 확인 불가 · 재시작 사용 불가';
    if(!configured){$('sshTarget').value=d.ssh_target||'';configured=true;}
  }catch(error){
    $('hero').className='hero error';$('stateText').textContent='상태 확인 오류';$('diagnosis').textContent=String(error);
    $('linkSummary').textContent='상세 진단 확인 불가';$('linkChecks').textContent=String(error);
    for(const id of ['physicalUsb','hudServer','hudPhysical','ip','comma','usb','usb3','service','frame','model','nativeStatus','ready','age','pipeline','web'])$(id).textContent='확인 불가';
    for(const id of ['stages','recentStages','negotiation','temperature','interface','storage','version','previewMetrics'])$(id).textContent='확인 불가';
    $('restart').disabled=$('reconnect').disabled=true;$('webOpen').hidden=true;
  }finally{refreshing=false;}
}
let lastCheck=null;
function saveCheck(format){
  if(!lastCheck)return;
  const text=format==='json'?JSON.stringify(lastCheck,null,2):$('checkResult').textContent;
  const url=URL.createObjectURL(new Blob([text],{type:format==='json'?'application/json':'text/plain;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download='jetson-'+lastCheck.action+'.'+format;a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('checkTxt').onclick=()=>saveCheck('txt');$('checkJson').onclick=()=>saveCheck('json');
async function action(name,extra={}){
  if(busy)return;
  const destructive=['restart','reconnect'].includes(name);
  if(destructive&&!confirm('Jetson 서비스를 재시작하면 영상·AI 연결이 잠시 끊깁니다. 안전하게 주차하고 진행할까요?'))return;
  busy=true;document.querySelectorAll('button[data-action]').forEach(button=>button.disabled=true);
  const diagnostic=name==='video_test'||name==='model';
  const output=$(diagnostic?'checkResult':'result');
  output.hidden=false;output.textContent=diagnostic?'검사 중…':'확인 중…';
  if(diagnostic){lastCheck=null;$('checkDownloads').hidden=true;}
  let http=null;
  try{
    const response=await fetch('/api/jetson/action',{method:'POST',headers:{'Content-Type':'application/json','X-Jetson-Token':token},
      body:JSON.stringify({action:name,confirm:destructive,...extra})});
    http=response.status;
    const text=await response.text();let value;
    try{value=JSON.parse(text);}catch{throw new Error(text.slice(0,400)||'응답 내용 없음');}
    if(!value||typeof value.ok!=='boolean')throw new Error('검사 결과 없음');
    if(diagnostic)lastCheck={...value,action:name,http};
    output.textContent=(response.ok&&value.ok?(value.note||'조회 완료'):'검사 실패 · HTTP '+http+' · '+(value.error||'원인 미확인'))
      +'\\n'+JSON.stringify(value,null,2);
  }catch(error){
    output.textContent='검사 실패 · '+(http?'HTTP '+http:'통신 오류')+' · '+String(error);
    if(diagnostic)lastCheck={action:name,http,ok:false,error:String(error)};
  }finally{
    if(diagnostic)$('checkDownloads').hidden=!lastCheck;
    busy=false;document.querySelectorAll('button[data-action]').forEach(button=>button.disabled=['restart','reconnect'].includes(button.id));
    await refresh();
  }
}
$('localDiagBtn').onclick=async()=>{
  $('localDiag').textContent='USB 및 네트워크를 읽기 전용으로 검사하고 있습니다…';
  try{
    const response=await fetch('/api/jetson/diagnostics',{cache:'no-store'});
    if(!response.ok)throw new Error('진단 응답 '+response.status);
    const data=await response.json(),u=data.usb_gadget||{},l=data.usb_link||{},j=data.jetlink||{},p=data.pipeline||{};
    const fmt=v=>v===true?'정상':v===false?'미연결 / 비활성':v==null?'확인 불가':String(v);
    $('localDiag').textContent=[
      '검사 시각: '+data.generated_at_utc,
      '통신 방식: '+fmt(data.mode?.transport),
      'USB UDC 바인딩: '+fmt(u.udc_bound),
      'UDC 상태: '+JSON.stringify(u.controllers||[]),
      'USB NCM 장치: '+fmt(u.ncm_interface),
      '관리 상태: '+fmt(l.usb_admin_state)+' / carrier '+fmt(l.usb_carrier),
      '콤마 USB IPv4: '+fmt(u.comma_ipv4)+' / 기대 '+fmt(u.expected_comma_ipv4),
      '젯슨 USB 경로: '+fmt(u.peer_route_via_usb),
      'Jetlink 실행: '+fmt(j.native_state),
      '젯슨 최근 응답: '+fmt(j.peer_telemetry_fresh),
      'NCM 자동 감지 carrier: '+fmt(u.ncm_carrier),
      'NCM 복구 상태: '+fmt(data.network_recovery?.state),
      '자동 진단: '+fmt(data.assessment?.code),
      '권장 조치: '+fmt(data.assessment?.recommended_action),
      '젯슨 원격 서비스(SSH 조회 후 60초): '+JSON.stringify(data.remote_service_check?.services||{}),
      '카메라 TX / RX: '+fmt(p.camera_tx_recent)+' / '+fmt(p.camera_rx_recent),
      '추론: '+fmt(p.inference),
      'HUD 데이터 TX: '+fmt(p.hud_tx_recent),
      'HUD 복구 / 실패 원인: '+JSON.stringify(data.hud_runtime?.recovery||{}),
      'HUD 프로세스 / 중단 단계: '+JSON.stringify({running:data.hud_runtime?.renderer_running,pid:data.hud_runtime?.renderer_pid,stage:data.hud_runtime?.stopped_stage}),
      'HUD 서비스 상태: '+JSON.stringify(data.hud_runtime?.hud_service||{}),
      '대기 화면 원인: '+fmt(data.hud_runtime?.waiting_reason),
      '마지막 인코딩 만료(초 전): '+fmt(data.hud_runtime?.encoder_expiry_age_s),
      '패널 간격 초과 횟수 / 최대 초: '+fmt(data.hud_runtime?.panel_gap_count)+' / '+fmt(data.hud_runtime?.panel_gap_max_s),
      '마지막 프레임 공백 (검은 화면 후보, 콤마 추정 시각): '+JSON.stringify(data.hud_runtime?.last_gap||{}),
      '마지막 차량 프레임 쓰기 (Jetson mono): '+fmt(data.hud_runtime?.last_vehicle_write_jetson_mono),
      '인코더 및 패널 이벤트: '+JSON.stringify(data.hud_runtime?.frame_events||[]),
      'USB 장치와 패널 명령 이력: '+JSON.stringify(data.hud_runtime?.panel_events||[]),
      'Jetson 1분 평균 부하: '+fmt(data.hud_runtime?.jetson_load_1m),
      '패널 쓰기 성공 보고 (실제 픽셀 미확인): '+fmt(data.hud_runtime?.panel_write_reported),
      '마지막 HUD 오류 시각 (Jetson Unix): '+fmt(data.hud_runtime?.last_error_time_unix),
      '최근 통신 오류: '+JSON.stringify(data.hud_runtime?.last_transport_error||{}),
      '설치 커밋: comma='+fmt(data.hud_runtime?.comma_revision)+' / jetson='+fmt(data.hud_runtime?.jetson_revision),
      'NCM 전송 오류: '+fmt(data.hud_runtime?.transport_error),
      '송신 준비·전송·왕복 시간: '+JSON.stringify(data.hud_runtime?.timing||{}),
      '수신 간격·저장 시간: '+JSON.stringify(data.hud_runtime?.receiver_timing||{}),
      '렌더링·패널 전송 시간: '+JSON.stringify(data.hud_runtime?.renderer_timing||{}),
      '실제 HUD 화면: 확인 불가 (원격 상태만으로 검증하지 않음)',
      '',
      '확인할 사항:',
      ...(data.observations||[]).map((item,i)=>(i+1)+'. '+item),
      '',
      data.notice||''
    ].join('\\n');
  }catch(error){$('localDiag').textContent='진단 실행 실패: '+String(error);}
};
async function refreshHistory(){
  try{
    const response=await fetch('/api/jetson/history',{cache:'no-store'});
    if(!response.ok)throw new Error('이력 API 응답 '+response.status);
    const data=await response.json(),events=data.events||[];
    $('linkHistory').textContent=events.length?events.slice(-30).reverse().map(item=>{
      const s=item.state||{};
      return (item.at||'?')+' · '+(s.code||'?')+' · USB '+(s.ncm_interface||'-')+
        ' · carrier '+(s.usb_carrier??'?')+' · peer '+(s.peer_recent??'?')+
        ' · 복구 '+(s.network_state||'?');
    }).join('\\n'):'아직 기록된 연결 상태 변화가 없습니다.';
    $('linkHistory').textContent+='\\n보관 '+(data.count||0)+'건 · 저장 방식 '+(data.persistence||'확인 불가');
  }catch(error){$('linkHistory').textContent='연결 이력 조회 실패: '+String(error);}
}
$('refreshHistory').onclick=refreshHistory;
refreshHistory();setInterval(refreshHistory,20000);
// UI-only Korean labels. Keep machine stage codes unchanged for API and safety gates.
const hudUpdateStageLabels={
  idle:'대기',
  checking:'업데이트 준비 및 확인 중',
  downloading:'업데이트 파일 다운로드 중',
  transferring:'젯슨으로 파일 전송 중',
  preflight:'설치 전 안전 점검 중',
  installing:'젯슨에 설치 중',
  verifying:'설치 결과 확인 중',
  completed:'설치 완료',
  failed:'설치 실패',
  unknown:'진행 상태 확인 필요'
};
const hudUpdateStageName=code=>hudUpdateStageLabels[code]||'상태 확인 필요';
let hudUpdateCommit='',hudUpdateInstalled='',hudUpdateRecheck=false,hudUpdateChecking=false,hudUpdateRateLimitReset=0;
let hudUpdateLastCheckAt=0,hudUpdateOnResume=false;
async function hudUpdateCall(path, payload){
  const response=await fetch(path,{method:'POST',cache:'no-store',
    headers:{'Content-Type':'application/json','X-Jetson-Token':token},
    body:JSON.stringify(payload)});
  const raw=await response.text();
  let data;
  try{data=JSON.parse(raw);}catch(_){throw new Error('HTTP '+response.status+': '+raw.slice(0,280));}
  if(!response.ok||!data.ok){
    const detail=data.ssh_preflight||{},code=detail.code||data.code||'';
    const message=data.error||detail.recommendation||'HTTP '+response.status;
    throw new Error((code?code+' · ':'')+message);
  }
  return data;
}
async function hudUpdateCheck(){
  if(hudUpdateChecking)return;
  hudUpdateChecking=true;
  hudUpdateLastCheckAt=Date.now();
  $('hudUpdateInstall').disabled=true;
  $('hudSummaryAvailable').textContent='확인 중';
  $('hudUpdateMessage').textContent='검증 패키지와 젯슨 설치 상태를 확인하고 있습니다.';
  $('hudUpdateState').textContent='GitHub 릴리스 및 젯슨 버전을 확인하고 있습니다…';
  try{
    const d=await hudUpdateCall('/api/jetson/hud-update/check',{});
    const r=d.release||{};
    hudUpdateCommit=r.commit||'';
    hudUpdateInstalled=d.installed||'';
    const unpublished=d.release_status==='not_published';
    const compatibleFallback=d.release_status==='compatible_fallback';
    const gitFallback=d.release_status==='git_fallback';
    const rateLimited=d.release_status==='rate_limited';
    hudUpdateRateLimitReset=rateLimited&&Number.isSafeInteger(d.rate_limit_reset)&&d.rate_limit_reset>0
      ?d.rate_limit_reset:0;
    hudUpdateRecheck=unpublished||compatibleFallback||gitFallback||d.release_status==='error'||rateLimited;
    hudUpdateOnResume=unpublished||compatibleFallback;
    const previous=d.job||{};
    const oldInstall=previous.stage==='completed'&&(!hudUpdateCommit||previous.commit!==hudUpdateCommit);
    const priorFailure=previous.stage==='failed'&&hudUpdateCommit&&hudUpdateInstalled===hudUpdateCommit;
    const releaseLabel=hudUpdateCommit||(unpublished
      ? '배포 대기 · 메인 '+(d.latest_commit||'확인 중') : '조회 오류 · 설치 불가');
    const lines=['최신 검증 패키지: '+releaseLabel,
      ...(compatibleFallback?['현재 메인 버전: '+(d.latest_commit||'확인 불가')+' (배포 대기)',
                             '선택한 패키지: 기존 설치보다 최신이며 메인까지 HUD/통신 경로 변경이 없는 검증본',
                             '최신 메인 패키지는 배포되면 다시 확인합니다.']
        :unpublished?['패키지 상태: 메인 코드의 검증/배포가 완료되지 않았거나 안전한 대체 패키지가 없습니다.',
                      '자동 재확인: 이 페이지가 열려 있으면 15분 간격으로 다시 확인합니다.']
                     :d.release_error?['패키지 확인: '+d.release_error]:[]),
      ...(gitFallback?['API 요청 한도 초과 · 공식 Git 브랜치와 HUD 릴리스 태그 일치 확인',
                       '설치 전 릴리스 ZIP·내부 파일 SHA256 검증과 차량 안전 검사를 다시 수행합니다.']:[]),
      ...(rateLimited?['자동 재확인: API 요청 제한이 해제된 이후에만 진행합니다.']:[]),
      ...(hudUpdateOnResume?['화면 복귀 시: 마지막 확인에서 2분 이상 지났다면 다시 확인합니다.']:[]),
      '젯슨 현재 버전: '+(d.installed||'확인 불가'),
      '업데이트 필요: '+(d.available===true?'예':d.available===false?'아니요':'확인 불가'),
      'SSH 연결: '+(d.ssh_configured?'설정됨':'미설정'),
      'SSH 조회 오류: '+(d.remote_error||'없음'),
      '차량 안전 확인: '+(d.can_install_safely===true?'충족':d.safety_reason||'확인 불가'),
      '작업 상태: '+(priorFailure?'이전 설치 실패 이력 · 현재 버전은 최신 (서비스 별도 확인)':oldInstall?'이전 버전 설치 완료':hudUpdateStageName(previous.stage||'idle')),
      '원격 접속 위치와 관계없이 콤마4의 실시간 안전 확인이 필요합니다.'];
    $('hudUpdateState').textContent=lines.join('\\n');
    $('hudSummaryRelease').textContent=d.release_ready===true&&hudUpdateCommit
      ?(compatibleFallback?'대체 검증본 · ':'검증 완료 · ')+hudUpdateCommit.slice(0,8)
      :(unpublished?'배포 대기':rateLimited?'조회 제한':'확인 필요');
    $('hudSummaryInstalled').textContent=hudUpdateInstalled?hudUpdateInstalled.slice(0,8):'확인 불가';
    $('hudSummaryAvailable').textContent=d.release_ready===true&&d.available===true?'업데이트 있음':
      d.available===false?'설치 불필요':'확인 필요';
    $('hudSummarySafety').textContent=!d.ssh_configured?'SSH 미설정':
      d.remote_error?'SSH 연결 오류':d.can_install_safely===true?'조건 충족':'차량 안전 확인 필요';
    $('hudUpdateMessage').textContent=priorFailure?'이전 설치 실패가 기록되어 있습니다. 상세 내역을 확인해 주세요.':
      oldInstall?'이전 버전 설치 기록이 있습니다.':'설치 전 차량의 안전 조건을 실시간으로 확인합니다.';
    const activeJob=previous.stage&&!['idle','completed','failed'].includes(previous.stage);
    $('hudUpdateInstall').disabled=!(hudUpdateCommit&&d.release_ready===true&&d.available===true&&d.ssh_configured&&!d.remote_error&&d.can_install_safely===true&&!activeJob);
  }catch(e){hudUpdateCommit='';hudUpdateInstalled='';hudUpdateRecheck=true;hudUpdateOnResume=false;
    $('hudSummaryRelease').textContent='확인 실패';$('hudSummaryInstalled').textContent='확인 불가';
    $('hudSummaryAvailable').textContent='확인 필요';$('hudSummarySafety').textContent='확인 필요';
    $('hudUpdateMessage').textContent='조회 오류가 발생했습니다. 상세 내역을 확인해 주세요.';
    $('hudUpdateState').textContent='확인 실패: '+String(e);}
  finally{hudUpdateChecking=false;}
}
async function hudUpdatePoll(){
  try{
    const r=await fetch('/api/jetson/hud-update/status',{cache:'no-store'});
    const d=await r.json(),j=d.job||{};
    if(j.stage&&j.stage!=='idle'&&j.stage!=='unknown'){
      const pastInstall=(j.stage==='completed'&&(!hudUpdateCommit||j.commit!==hudUpdateCommit))||
        (j.stage==='failed'&&hudUpdateCommit&&hudUpdateInstalled===hudUpdateCommit);
      $('hudUpdateJob').textContent=(pastInstall?'이전 설치 이력 (현재 최신 패키지 확인과 별개)\\n':'')+
        '진행 단계: '+hudUpdateStageName(j.stage)+'\\n커밋: '+(j.commit||'')+
        '\\n시간: '+(j.updated?new Date(j.updated*1000).toLocaleString():'')+
        (j.total_bytes>0?'\\n다운로드: '+j.received_bytes+' / '+j.total_bytes+' bytes':'')+
        (j.failed_stage?'\\n실패 단계: '+hudUpdateStageName(j.failed_stage):'')+'\\n'+(j.detail||'');
      if(!['completed','failed'].includes(j.stage)){
        $('hudUpdateInstall').disabled=true;
        $('hudSummaryAvailable').textContent='설치 진행 중';
        $('hudUpdateMessage').textContent='설치 단계: '+hudUpdateStageName(j.stage);
      }else if(pastInstall){
        $('hudUpdateMessage').textContent='이전 설치 기록이 있습니다. 상세 내역을 확인해 주세요.';
      }else{
        $('hudUpdateMessage').textContent=j.stage==='failed'?'설치 실패 · 상세 내역을 확인해 주세요.':
          '설치 완료 기록 · 실제 HUD 출력은 별도 확인이 필요합니다.';
      }
    }else if(j.stage==='unknown'){
      $('hudUpdateJob').textContent='확인 필요: '+(j.detail||'작업 상태 확인 실패');
      $('hudSummaryAvailable').textContent='작업 상태 불명';
      $('hudUpdateMessage').textContent='작업 상태 확인 실패로 설치를 중단합니다.';
      $('hudUpdateInstall').disabled=true;
    }
  }catch(_){}
}
$('hudUpdateCheck').onclick=hudUpdateCheck;
$('hudUpdateInstall').onclick=async()=>{
  if(!hudUpdateCommit)return;
  if(!confirm('차량이 P단에 완전 정지해 있고 주행 제어가 꺼져 있으며 업데이트가 완료될 때까지 P단과 젯슨 전원이 유지됩니까? (Offroad도 가능)\\n'+
              'HUD 영상이 중단될 수 있습니다. 검증된 '+hudUpdateCommit.slice(0,12)+' 버전을 설치하시겠습니까?'))return;
  $('hudUpdateInstall').disabled=true;
  $('hudSummaryAvailable').textContent='시작 요청 중';
  $('hudUpdateMessage').textContent='안전 조건을 확인하며 설치 시작을 요청하고 있습니다.';
  $('hudUpdateState').textContent='업데이트 시작 요청 중…';
  try{
    const d=await hudUpdateCall('/api/jetson/hud-update/start',{confirm:true,commit:hudUpdateCommit});
    $('hudUpdateState').textContent=d.message||'업데이트 작업이 시작되었습니다.';
  }catch(e){$('hudUpdateState').textContent='시작 실패: '+String(e);}
  await hudUpdatePoll();
};
setInterval(hudUpdatePoll,5000);hudUpdatePoll();
// Respect GitHub quota reset. Unpublished releases are retried at low frequency.
setInterval(()=>{if(hudUpdateRecheck&&token&&!hudUpdateChecking&&document.visibilityState!=='hidden'&&
  (!hudUpdateRateLimitReset||Date.now()>=hudUpdateRateLimitReset*1000))hudUpdateCheck();},900000);
// A tab returning to the foreground can discover a just-published release.
// Bound foreground checks to once every two minutes; never trigger installation.
function hudUpdateCheckOnResume(){
  if(hudUpdateOnResume&&token&&!hudUpdateChecking&&document.visibilityState!=='hidden'&&
    Date.now()-hudUpdateLastCheckAt>=120000&&
    (!hudUpdateRateLimitReset||Date.now()>=hudUpdateRateLimitReset*1000))hudUpdateCheck();
}
document.addEventListener('visibilitychange',hudUpdateCheckOnResume);
if(typeof window!=='undefined')window.addEventListener('focus',hudUpdateCheckOnResume);
let experimentTimer, experimentBusy=false, parkedAiBusy=false;
async function engineExperiment(start){
  if(experimentBusy)return;
  if(start&&!confirm('완전 Offroad에서 합성 입력 엔진 시험을 시작할까요? 주행용 AI는 활성화되지 않습니다.'))return;
  clearTimeout(experimentTimer);
  experimentBusy=true;
  $('experimentStart').disabled=true;
  try{
    const data=await hudUpdateCall('/api/jetson/engine-experiment', start?
      {action:'start',confirm:true,comma_user:$('experimentUser').value.trim(),
       identity_file:$('experimentIdentity').value.trim()||null,trial:$('experimentTrial').value||null}:{action:'status'});
    const r=data.experiments||data.experiment||{};
    const rows=(r.trials||[]).map(v=>{
      const t=v.synchronized_inference_ms||{};
      return [v.name,v.state,v.failed_phase?'실패 단계 '+v.failed_phase:'',v.failure_diagnosis?.category||'',
        v.verbose_log_path?'VERBOSE 로그 기록 있음':'',v.error,v.synthetic_fps?'합성 FPS '+v.synthetic_fps.toFixed(1):'',
        v.measured_runs?'평균/p95/최대 ms '+[t.mean,t.p95,t.max].join('/'):''].filter(Boolean).join(' · ');
    });
    $('experimentResult').textContent=[r.available===false?'시험 기록 없음':r.state,r.current_trial,r.error,...rows].filter(Boolean).join('\\n');
    if(['accepted','running'].includes(r.state))experimentTimer=setTimeout(()=>engineExperiment(false),5000);
  }catch(e){$('experimentResult').textContent=e.message;}
  finally{experimentBusy=false;}
}
$('experimentStart').onclick=()=>engineExperiment(true);
$('experimentStatus').onclick=()=>engineExperiment(false);
$('parkedAiCheck').onclick=async()=>{
  if(parkedAiBusy)return;
  if(!confirm('차량이 P단에 완전히 정지해 있고 오픈파일럿 주행 제어가 꺼져 있습니까?\\n엔진 생성 없이 AI 준비 상태만 읽기 전용으로 확인합니다.'))return;
  parkedAiBusy=true;
  $('parkedAiCheck').disabled=true;
  $('parkedAiResult').textContent='실시간 P단 안전 조건과 젯슨 AI 준비 상태를 확인하고 있습니다…';
  try{
    const d=await hudUpdateCall('/api/jetson/engine-experiment',{action:'parked_check'});
    const r=d.inference_readiness||{}, errors=r.engine_build_errors||[];
    const modelPrefix=typeof r.installed_sha256==='string'?r.installed_sha256.slice(0,16):'';
    const currentErrors=modelPrefix?errors.filter(v=>typeof v.file==='string'&&v.file.startsWith(modelPrefix+'.')):[];
    const currentBuild=currentErrors.length?currentErrors[currentErrors.length-1]:null;
    const failureCategory=currentBuild?.failure_diagnosis?.category||'';
    const failureLabel={
      conv_gelu_tactic_unavailable:'Conv/GELU 연산 구현 선택 실패',
      tactic_unavailable:'TensorRT 연산 구현 선택 실패',
      memory_allocation_failed:'메모리 할당 실패',
      onnx_parse_failed:'ONNX 모델 해석 실패'
    }[failureCategory]||failureCategory||'원인 추가 분석 필요';
    $('parkedAiResult').textContent=[
      '정차 확인: '+(d.parking_evidence||'검증 불가'),
      '콤마4·젯슨 AI 모델 SHA 일치: '+(d.model_sha_match===true?'예':d.model_sha_match===false?'불일치':'확인 불가'),
      '모델 파일·구성: '+(r.model_file_ready===true?'정상':'추가 확인 필요'),
      '현재 모델 엔진: '+(r.engine_file_present===true?'파일 있음':'없음/미검증'),
      '과거 합성 GPU 시험: '+(r.synthetic_inference_verified===true?'검증됨':'미검증'),
      'USB-NCM 최근 상태: '+(r.ncm_fresh_ready===true?'정상':'신선도 미충족/미검증'),
      '추가 조치: '+((r.blockers||[]).join(', ')||'준비 상태 차단 항목 없음'),
      '현재 모델 빌드 오류: '+(currentBuild?'실패 기록 있음 · '+failureLabel:'현재 모델의 오류 기록 없음'),
      '근본 원인 확정: '+(currentBuild?.failure_diagnosis?.root_cause_proven===true?'예':'아니요 · 독립 실험 필요'),
      '다음 단계: 완전 Offroad(IsOffroad=1, IsOnroad=0)에서 native_fp16 독립 시험부터 시작',
      '읽기 전용 점검 완료 · 엔진 생성/실제 GPU 추론/주행 제어 변경 없음'
    ].join('\\n');
  }catch(e){$('parkedAiResult').textContent='P단 AI 진단 실패: '+String(e.message||e);}
  finally{parkedAiBusy=false;$('parkedAiCheck').disabled=!token;}
};
$('experimentSsh').onclick=async()=>{
  $('experimentSsh').disabled=true;
  try{
    const d=await hudUpdateCall('/api/jetson/engine-experiment',{action:'ssh',comma_user:$('experimentUser').value.trim(),
      identity_file:$('experimentIdentity').value.trim()||null});
    const p=d.ssh_preflight||{};
    $('experimentSshResult').textContent=[
      '콤마 → 젯슨: '+(d.comma_to_jetson_verified?'확인':'미검증'),
      '젯슨 → 콤마: '+(p.ok?'인증 확인':p.code||'실패'),
      ...(p.code==='SSH_HOST_KEY_UNKNOWN'?['SSH 안내: 아래 호스트키 후보 확인 → 콤마4 본체에서 지문 별도 대조 → 명시적 등록 순으로 진행하세요. 보안 검사 우회 금지.']:[]),
      ...(p.code==='SSH_NO_IDENTITY'||p.credential_preflight?.identity_candidate_present===false?['SSH 로그인키도 준비되지 않았습니다. 젯슨의 승인된 개인키 경로를 지정하고 그 공개키가 콤마4에 등록돼야 합니다.']:[]),
      'USB 직접 경로: '+(p.usb_ncm?.ok===true?'검증 완료':p.ncm_reason||'추가 확인 필요')
        +' · 장치 '+(p.usb_ncm?.interface||p.route_observed?.dev||'미확인')
        +' · 출발지 '+(p.usb_ncm?.source||p.route_observed?.src||p.route_observed?.prefsrc||'미확인')
        +(p.usb_ncm?.source_verification?' · 근거 '+p.usb_ncm.source_verification:''),
      '젯슨 기존 SSH 키 후보: '+(p.credential_preflight?.identity_candidate_present===true?'있음':'없음/미검증')
        +' · SSH agent: '+(p.credential_preflight?.ssh_agent_present===true?'있음':'없음')
        +' · known_hosts: '+(p.credential_preflight?.known_hosts_present===true?'있음':'없음/미검증'),
      p.recommendation||'',
      '엔진 시험 Offroad 조건: '+(p.offroad_allowed?'충족':'미충족/미검증'),
      '실행 계정 HOME: '+(p.client?.client_home||'확인 불가'),
      ...(p.client?.identity_files||[]).map(k=>k.path+' · 존재 '+k.exists+' · 읽기 '+k.readable+' · 공개키 지문 '+(k.public_key?.fingerprints||[]).join(', ')),
      ...(p.client?.known_hosts||[]).map(k=>'known_hosts: '+k.path+' · 존재 '+k.exists)
    ].join('\\n');
  }catch(e){$('experimentSshResult').textContent=e.message;}
  finally{$('experimentSsh').disabled=false;}
};
async function commaHostKey(enroll){
  const output=$('experimentHostResult'),id=enroll?'experimentHostEnroll':'experimentHostPreview';
  const fp=$('experimentHostFingerprint').value.trim();
  if(enroll){
    if(!/^SHA256:[A-Za-z0-9+/]{43}$/.test(fp)){
      output.textContent='콤마4 장치에서 직접 검증한 SHA256 호스트 공개키 지문을 입력해 주세요.';return;
    }
    if(!confirm('콤마4 SSH 서버의 호스트 공개키 지문을 USB 관측값과 별도로 직접 대조하셨습니까?\\n일치하는 공개키만 젯슨 known_hosts에 등록하며 기존 키는 덮어쓰지 않습니다. 완전 Offroad가 필요합니다.'))return;
  }
  $(id).disabled=true;
  output.textContent='고정 USB 경로의 SSH 공개키를 '+(enroll?'독립 검증·등록':'읽기 전용 확인')+' 중입니다…';
  try{
    const data=await hudUpdateCall('/api/jetson/engine-experiment',{
      action:enroll?'ssh_key_enroll':'ssh_key_preview',comma_user:$('experimentUser').value.trim(),
      ...(enroll?{expected_fingerprint:fp,confirm:true}:{})});
    const v=data.host_key||{};
    output.textContent=[
      '작업: '+(enroll?'명시적으로 검증한 키 등록':'공개키 후보 읽기'),
      '상태: '+(v.code||'확인 불가'),
      '지문: '+(v.candidate_fingerprint||v.fingerprint||'확인 불가'),
      '출처: '+(v.source||'지정된 USB 호스트'),
      '등록 변경: '+(v.settings_changed===true?'예':'아니요'),
      v.recommendation||'',
      '호스트키 신뢰와 SSH 사용자 인증은 별개입니다. 키를 등록한 뒤 「젯슨 → 콤마 SSH 확인」을 다시 누르세요.'
    ].filter(Boolean).join('\\n');
  }catch(e){output.textContent='작업 중단: '+String(e.message||e);}
  finally{$(id).disabled=false;}
}
$('experimentHostPreview').onclick=()=>commaHostKey(false);
$('experimentHostEnroll').onclick=()=>commaHostKey(true);
let lteChangeAllowed=false, lteConfigured=false, lteProbeReady=false;
function lteButtons(){
  $('lteEnable').disabled=!lteChangeAllowed||lteConfigured;
  $('lteDisable').disabled=!lteChangeAllowed||!lteConfigured;
  $('lteProbe').disabled=!lteChangeAllowed||!lteProbeReady;
}
async function lteShare(action){
  if(action!=='status'&&!lteChangeAllowed){$('lteResult').textContent='먼저 LTE 공유 상태를 확인해 주세요. 시동이 켜진 P단에서는 변경할 수 없습니다.';return;}
  if((action==='enable'&&lteConfigured)||(action==='disable'&&!lteConfigured)||(action==='probe'&&!lteProbeReady)){
    $('lteResult').textContent='현재 공유 설정에 맞지 않는 작업입니다. 먼저 LTE 공유 상태를 다시 확인해 주세요.';return;
  }
  if(action!=='status'&&!confirm(action==='probe'?'Offroad 상태에서 USB 경로로 DNS·HTTPS 시험 트래픽을 전송합니다. 설정은 변경하지 않습니다. 계속하시겠습니까?':'시동을 끄고 차량이 완전히 Offroad 상태입니까?\\n콤마4 LTE 데이터가 젯슨에 공유되며 데이터 요금이 발생할 수 있습니다.'))return;
  $('lteResult').textContent='LTE 공유 '+(action==='status'?'상태 확인':'변경')+' 중…';
  $('lteEnable').disabled=true;$('lteDisable').disabled=true;$('lteProbe').disabled=true;
  try{
    const data=await hudUpdateCall('/api/jetson/lte-share',{action:action,confirm:action!=='status'});
    if(action==='status'){
      const v=data.gateway||{};
      lteChangeAllowed=v.can_change===true;
      lteConfigured=v.configured===true;
      lteProbeReady=v.operational===true;
      $('lteResult').textContent=[
        '공유 변경 가능: '+(lteChangeAllowed?'예 · 완전 Offroad':'아니요 · 시동 OFF 및 완전 Offroad 필요'),
        '현재 설정: '+(lteConfigured?'설정됨':'미설정')+' · 규칙 설치: '+(v.enabled===true?'예':'아니요'),
        '실제 공유 경로: '+(v.operational===true?'정상 후보 · DNS/HTTPS 확인 필요':'미설정 또는 사용 불가 · 아래 원인 확인'),
        '콤마4 포워딩: '+(v.comma_forwarding??'-'),
        'NAT/포워딩 규칙: '+JSON.stringify(v.firewall_rules||[]),
        '젯슨 USB 기본 경로: '+(v.jetson_usb_route??'-'),
        'LTE 인터페이스: '+(v.active_cellular_wan||v.wan||'-'),
        '실제 기본 인터넷 경로: '+((v.uplink_diagnostics||{}).default_interface||'미확인')
          +' · 기본 경로가 Wi-Fi라도 LTE PPP 정책 검증 없이는 공유 금지',
        'LTE 경로: '+(v.route_mode||'미검증')+' · '+((v.uplink_diagnostics||{}).reason||'기존 기본 경로 확인'),
        'LTE 세부 검증 단계: '+((v.uplink_diagnostics||{}).failed_step||'확인 필요'),
        'PPP 정책 라우트 확인: '+JSON.stringify((v.uplink_diagnostics||{}).policy_route||{}),
        '방화벽 도구: '+(v.firewall_backend||'-'),
        'USB 인터넷·DNS: '+(v.internet_dns_verified===true?'검증 성공 · 120초 이내':'미검증 · 검사 버튼으로 확인'),
        '복구 대기: '+(v.recovery_pending?'예 · Offroad에서 끄기/재시도':'아니요'),
        ...(v.failures||[]).map(f=>f.code+': '+f.detail),
        v.change_requirement||'',
        v.update_note||'콤마4가 HUD 업데이트를 내려받는 방식은 젯슨 Wi-Fi나 LTE 공유를 별도로 요구하지 않습니다.',
        v.next_action||'',
        'LTE 실제 인터넷·DNS 성공은 별도 확인해야 합니다.',
        v.jetson_error||v.note||v.message||''
      ].join('\\n');
    }else if(action==='probe'){
      const p=data.probe||{};
      $('lteResult').textContent=[
        'USB DNS: '+(p.usb_dns_success===true?'성공':'실패'),
        'USB HTTPS: '+(p.usb_https_success===true?'성공':'실패'),
        '시스템 DNS: '+(p.system_dns_success===true?'성공':'실패'),
        'LTE USB 실측: '+(p.internet_dns_verified===true?'성공':'미검증/실패'),
        ...(p.failures||[]).map(f=>f.code+': '+f.detail),
        '기존 경로·DNS·방화벽 설정은 변경하지 않았습니다.'
      ].join('\\n');
    }else{
      $('lteResult').textContent=data.message||'요청 완료. 상태를 다시 확인해 주세요.';
    }
    if(action!=='status'){
      // Always refresh the backend status after any mutation or probe.
      lteChangeAllowed=false;lteConfigured=false;lteProbeReady=false;
      $('lteResult').textContent+='\\nLTE 공유 상태 확인을 다시 눌러 최신 설정을 확인해 주세요.';
    }
  }catch(e){
    lteChangeAllowed=false;lteConfigured=false;lteProbeReady=false;
    $('lteResult').textContent='실행 실패: '+String(e);
  }finally{
    lteButtons();
  }
}
$('lteStatus').onclick=()=>lteShare('status');
$('lteEnable').onclick=()=>lteShare('enable');
$('lteDisable').onclick=()=>lteShare('disable');
$('lteProbe').onclick=()=>lteShare('probe');
$('refresh').onclick=refresh;
document.querySelectorAll('button[data-action]').forEach(button=>button.onclick=()=>action(button.dataset.action));
$('saveTarget').onclick=()=>action('configure',{ssh_target:$('sshTarget').value.trim()});

async function bundleOffroadEngineTrial(start){
  if(bundleTrialBusy)return;
  if(start&&!confirm('시동을 끈 완전 Offroad 상태이며 외부 AI가 꺼져 있습니까?\\n현재 모델의 native_fp16 독립 TensorRT 엔진 시험을 시작합니다. GPU 자원을 사용하지만 주행용 AI는 자동 활성화되지 않으며 결과는 이후 통합진단 ZIP에 포함됩니다. 계속하시겠습니까?'))return;
  bundleTrialBusy=true;
  $('bundleTrialStart').disabled=true;
  clearTimeout(bundleTrialPoll);
  const output=$('bundleTrialResult');
  output.textContent=start?'안전 조건과 역방향 SSH를 검증하고 독립 엔진 시험 시작을 요청합니다…':'기존 독립 엔진 시험 상태를 읽는 중입니다…';
  try{
    const d=await hudUpdateCall('/api/jetson/engine-experiment',start?
      {action:'start',confirm:true,trial:'native_fp16',comma_user:$('experimentUser').value.trim(),
       identity_file:$('experimentIdentity').value.trim()||null}:{action:'status'});
    const v=d.experiments||d.experiment||{};
    const trials=Array.isArray(v.trials)?v.trials:[];
    output.textContent=[
      start?'native_fp16 독립 시험 시작 요청이 접수되었습니다.':'젯슨 시험 이력 조회 완료',
      '시험 기록: '+(v.available===false?'아직 없음':v.available===true?'있음':'시작 요청 또는 진행 상태 확인 필요'),
      '상태: '+(v.state||'미검증'),
      '현재 시험: '+(v.current_trial||'미확인'),
      '모델 SHA: '+(v.model_sha256||'확인 불가'),
      '현재 모델 GPU 합성 검증: '+(v.native_gpu_verified===true?'성공 기록 있음 · 실제 주행 검증 아님':'미검증'),
      ...trials.map(t=>[t.name,t.state,t.failed_phase?'실패 단계 '+t.failed_phase:'',
        t.failure_diagnosis?.category||'',t.error||''].filter(Boolean).join(' · ')),
      v.error||'',
      '독립 시험 종료 후 아래 「젯슨 통합진단 ZIP 다운로드」를 다시 누르시면 11_offroad_ai_engine_trial.txt/json에 기록됩니다.'
    ].filter(Boolean).join('\\n');
    if(start||['accepted','running'].includes(v.state)){
      bundleTrialPoll=setTimeout(()=>bundleOffroadEngineTrial(false),5000);
    }
  }catch(error){
    output.textContent='시험 '+(start?'시작':'조회')+' 실패: '+String(error.message||error)+
      '\\nSSH 호스트키 신뢰·로그인 인증과 완전 Offroad 조건을 확인하세요. 통합진단 ZIP은 별도로 내려받을 수 있습니다.';
  }finally{bundleTrialBusy=false;}
}
$('bundleTrialStart').onclick=()=>bundleOffroadEngineTrial(true);
$('bundleTrialStatus').onclick=()=>bundleOffroadEngineTrial(false);

$('bundleDownload').onclick=async()=>{
  const button=$('bundleDownload'),note=$('bundleResult');
  if(!token){note.textContent='젯슨 상태 연결을 확인한 후 다시 눌러 주세요.';return;}
  button.disabled=true;
  note.textContent='진단 자료를 수집하고 있습니다. 다른 파일을 따로 저장하지 않으셔도 됩니다.';
  try{
    const response=await fetch('/api/jetson/diagnostics/bundle',{
      method:'POST',cache:'no-store',
      headers:{'Content-Type':'application/json','X-Jetson-Token':token},
      body:'{}'
    });
    if(!response.ok){
      const msg=await response.text();
      throw new Error('HTTP '+response.status+' · '+msg.slice(0,250));
    }
    const data=await response.blob();
    if(!data.size)throw new Error('다운로드 파일이 비어 있습니다.');
    const disposition=response.headers.get('Content-Disposition')||'';
    const match=disposition.match(/filename="([^"]+)"/);
    const filename=match?match[1]:'nexo-jetson-unified.zip';
    const url=URL.createObjectURL(data),link=document.createElement('a');
    link.href=url;link.download=filename;document.body.appendChild(link);
    link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),5000);
    note.textContent='통합진단 파일 저장 요청 완료: '+filename+' · 진단 결과는 ZIP 안의 00_guide.txt를 확인해 주세요.';
  }catch(error){
    note.textContent='통합진단 다운로드 실패: '+String(error);
  }finally{button.disabled=false;}
};

let aiPerformanceBusy=false,aiPerformanceReceived=0,aiProofExpires=0;
function clearAiPerformance(){
  $('aiLiveState').textContent='AI 실행 미확인 · 관찰 대기';
  $('aiLiveMetrics').textContent='최신 측정값 없음';
}
async function refreshAiPerformance(){
  if(aiPerformanceBusy||document.visibilityState==='hidden')return;
  aiPerformanceBusy=true;
  const started=performance.now();
  try{
    const response=await fetch('/api/jetson/ai-performance',{cache:'no-store',signal:AbortSignal.timeout(2000)});
    if(!response.ok)throw new Error('HTTP '+response.status);
    const data=await response.json();
    aiProofExpires=started+Math.max(0,Math.min(2.25,Number(data.inference_valid_for_s)||0))*1000;
    const active=data.external_inference_proven===true&&performance.now()<aiProofExpires;
    if(!active)for(const key of ['inference_fps','last_execution_ms','last_roundtrip_ms','roundtrip_p95_ms','roundtrip_max_ms','window_count'])data[key]=null;
    const metric=(value,unit)=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(2)+' '+unit:'미측정';
    $('aiLiveState').textContent=active?'JETSON AI ON · 최근 완료 추론 확인':
      data.model_ready===true?'AI 준비 · 실제 추론 대기':'AI 실행 미확인 · 관찰 대기';
    $('aiLiveMetrics').textContent=[
      '완료 추론: '+metric(data.inference_fps,'FPS'),
      '최근 실행: '+metric(data.last_execution_ms,'ms')+' / 왕복: '+metric(data.last_roundtrip_ms,'ms'),
      'HUD 연결 상태요청 왕복: '+metric(data.display_control_roundtrip_ms,'ms')+' (순수 전송 지연 아님)',
      '왕복 p95: '+metric(data.roundtrip_p95_ms,'ms')+' / 최대: '+metric(data.roundtrip_max_ms,'ms')+
        ' / 표본: '+metric(data.window_count,'개'),
      'GPU 전체 부하: '+metric(data.gpu_load_pct,'%')+' / 클럭: '+metric(data.gpu_clock_mhz,'MHz'),
      data.gpu_sample_fresh?'GPU 표본 경과: '+metric(data.gpu_sample_age_s,'초'):'GPU 표본 없음·만료 · SSH/관측 서비스 확인',
      data.gpu_error||''
    ].filter(Boolean).join('\\n');
    aiPerformanceReceived=performance.now();
  }catch(_error){clearAiPerformance();aiPerformanceReceived=0;}
  finally{aiPerformanceBusy=false;}
}
setInterval(()=>{
  if(!aiPerformanceReceived||performance.now()-aiPerformanceReceived>2250)clearAiPerformance();
  else if(performance.now()>=aiProofExpires&&$('aiLiveState').textContent.startsWith('JETSON AI ON'))clearAiPerformance();
},250);
document.addEventListener('visibilitychange',()=>{clearAiPerformance();aiPerformanceReceived=0;if(!document.hidden)refreshAiPerformance();});
refreshAiPerformance();setInterval(refreshAiPerformance,2000);
refresh();setInterval(refresh,2000);
</script></body></html>'''

GUIDE = '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jetson 설치 가이드 · XPlus</title>''' + STYLE + '''</head><body><main>
<header><h1>Windows에서 Jetson 설치</h1><a href="/jetson">실행 상태로 돌아가기</a></header>
<section class="card"><h2>설치 전 준비</h2><p class="sub">Orin Nano Super 기본 보드, Windows 10/11 64비트,
PC 여유 공간 약 70GB, 64GB 이상 microSD와 USB 리더를 준비합니다. NVMe는 지원 USB 케이스로 PC에 연결합니다.</p>
<p><strong>이미 가이드대로 설치했다면 재설치할 필요 없이 연결 확인부터 진행하세요.</strong></p></section>
<section class="card"><h2>설치 순서</h2><ol>
<li>원본 가이드의 설치 파일을 받고 압축을 모두 풉니다. CarrotJetson 폴더의 설치안내.html을 확인합니다.</li>
<li><code>01_설치준비.cmd</code>로 이미지 준비와 검증을 완료합니다.</li>
<li><code>02_SD카드설치.cmd</code>에서 대상 외장 저장장치의 이름·용량을 확인하고 설치합니다.
<strong>선택한 장치의 데이터가 지워지므로 백업과 디스크 선택을 확인해야 합니다.</strong></li>
<li>NVMe 사용 시 원문에 지정된 이미지와 공용 패치 버전을 확인하고 <code>03_SD_SSD공용패치.cmd</code>를 진행합니다.
원문의 시험 패치 조건과 부팅 검증 범위를 먼저 읽습니다.</li>
<li>저장장치를 안전하게 제거하고 Jetson을 완전히 끈 뒤 장착합니다. USB3 데이터 케이블로 comma4를 연결하고
보드에 맞는 별도 전원을 공급합니다.</li></ol></section>
<section class="card"><h2>XPlus에서 확인</h2><ol>
<li>7000의 Jetson 실행 화면에서 연결·USB3 협상 속도·서비스·마지막 통신을 확인합니다.
USB 표시 기능은 기존 <code>NexoJetsonUsb</code> 선택을 사용합니다. 모델 오프로딩 소유자가 활성화돼 있으면
표시 소유자가 같은 USB를 차지하지 않습니다.</li>
<li>기본 carrot 이미지의 서비스는 <code>carrot-jetlink.service</code>입니다. 기존 XPlus YOLO나 USB HUD 서비스도 별도로 감지합니다.
5600 웹 메뉴가 없어도 설치 실패를 뜻하지 않습니다.</li>
<li>모델 준비와 실행은 장치 연결과 별개입니다. 이미지에 저장된 엔진이 XPlus 네이티브 모델 계약과 다르면
기존 XPlus 설치 절차로 정확한 모델을 준비해야 합니다. 이 관리 메뉴는 모델을 자동 교체하지 않습니다.</li>
<li>로그·서비스 재시작에는 원본 가이드의 SSH 공개키 등록을 완료한 뒤 comma의 기존 SSH 설정에
사용자·호스트·키·known_hosts를 등록합니다. 7000에는 SSH 별칭 또는 사용자@호스트만 저장합니다.</li>
<li>차량 OFF/ON, USB 분리·재연결, Jetson 재부팅 후 자동 복구를 실제 장비에서 확인합니다.
offroad에서는 영상이나 추론이 대기 상태일 수 있습니다.</li></ol></section>
<section class="card"><h2>연결이 안 될 때</h2><p class="sub">전원, USB3 데이터 케이블, 실제 협상 속도, 서비스 자동 시작,
최근 통신과 오류를 차례로 확인합니다. Wi-Fi 정보 자동 전달은 호환되는 당근 확장이 필요하므로
XPlus에서 자동 설정 성공을 가정하지 않습니다. 기존 Wi-Fi 설정을 유지하고 네트워크 상태를 확인하세요.</p>
<a href="https://github.com/ajouatom/carrot-jetson/blob/main/docs/INSTALL-WINDOWS-KO.md" target="_blank" rel="noopener noreferrer">원본 설치 가이드 보기</a>
</section></main></body></html>'''
