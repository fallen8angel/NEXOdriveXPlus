"""Jetson runtime and installation screens; no device control in the guide."""

STYLE = '''<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif}*{box-sizing:border-box}
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
</style>'''

PAGE = '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jetson 관리 · XPlus</title>''' + STYLE + '''</head><body><main>
<header><h1>NVIDIA Jetson</h1><div class="actions"><a href="/jetson/install">Jetson 설치 가이드</a><a href="/">7000 홈</a></div></header>
<section id="hero" class="hero"><div id="stateText" class="state">연결 확인 중…</div>
<div id="diagnosis" class="sub">Jetson 상태를 기다리고 있습니다.</div></section>
<section class="card"><h2>Jetson → USB → Jetlink → Camera → Inference → HUD</h2>
<div id="stages" class="stages" aria-live="polite">단계별 상태 확인 중</div>
<p id="recentStages" class="sub">최근 heartbeat · 영상 · 추론 확인 중</p>
<p id="negotiation" class="sub">기능 협상 확인 중</p></section>
<section class="grid">
<div class="card"><div class="label">Jetson IP</div><div id="ip" class="value">확인 중</div></div>
<div class="card"><div class="label">comma4 연결</div><div id="comma" class="value">확인 중</div></div>
<div class="card"><div class="label">USB 연결</div><div id="usb" class="value">확인 중</div></div>
<div class="card"><div class="label">USB3 / 실제 협상 속도</div><div id="usb3" class="value">확인 중</div></div>
<div class="card"><div class="label">서비스 실행</div><div id="service" class="value">확인 중</div></div>
<div class="card"><div class="label">영상 / 최근 프레임</div><div id="frame" class="value">확인 중</div></div>
<div class="card"><div class="label">YOLO / AI 모델 실행</div><div id="model" class="value">확인 중</div></div>
<div class="card"><div class="label">모델 준비</div><div id="ready" class="value">확인 중</div></div>
<div class="card"><div class="label">마지막 통신</div><div id="age" class="value">확인 중</div></div>
<div class="card"><div class="label">오류</div><div id="error" class="value">확인 중</div></div>
<div class="card"><div class="label">연결 경로 / 모델</div><div id="pipeline" class="value">확인 중</div></div>
<div class="card"><div class="label">5600 웹 관리</div><div id="web" class="value">확인 중</div></div>
<div class="card"><div class="label">Jetson 온도 / thermal</div><div id="temperature" class="value">확인 중</div></div>
<div class="card"><div class="label">연결방식 / 인터페이스</div><div id="interface" class="value">확인 중</div></div>
<div class="card"><div class="label">저장장치 상태</div><div id="storage" class="value">확인 중</div></div>
<div class="card"><div class="label">Jetson 버전</div><div id="version" class="value">확인 중</div></div>
</section>
<section class="card section"><h2>Preview 전송 관찰</h2><p id="previewMetrics" class="sub">최근 preview 측정값 대기</p>
<p class="sub">Camera TX는 송신 완료입니다. Jetson의 수신 확인은 “영상 수신 테스트”에서 별도로 확인합니다.
carrot 연결은 작은 road preview부터 시작하며, 측정값이 없으면 정상으로 추정하지 않습니다.</p></section>
<section class="card section"><h2>연결과 상태 확인</h2><div class="actions">
<button id="refresh">상태 새로고침</button><button data-action="status">Jetson 연결 확인</button>
<button data-action="diagnose">통신 진단</button><button data-action="video_test">영상 수신 테스트</button>
<button data-action="model">모델 상태 · 준비 확인</button><a id="webOpen" hidden target="_blank" rel="noopener noreferrer">Jetlink 열기</a>
</div><p class="sub">기본 carrot Windows 이미지에는 5600 웹 관리가 없을 수 있습니다. USB 상태와 별도로 확인합니다.
영상이 없는 offroad 상태에서는 영상 수신과 추론을 정상으로 추정하지 않습니다.</p></section>
<section class="card section"><h2>Jetson 관리</h2><div class="actions">
<button data-action="logs">Jetson 로그 확인</button><button data-action="check_update">Jetson 업데이트 확인</button>
<button data-action="settings">Jetson 설정 확인</button><button class="danger" id="restart" data-action="restart" disabled>서비스 재시작</button>
<button class="danger" id="reconnect" data-action="reconnect" disabled>Jetson 재연결</button>
</div><p id="restartHint" class="sub">재시작은 정차 상태와 모델 비활성이 확인될 때 사용할 수 있습니다.</p>
<p class="sub">키 기반 SSH가 필요합니다. Jetson SSH 공개키와 known_hosts를 먼저 등록하고 기존 SSH 별칭 또는 사용자@호스트를 입력하세요.
계정과 IP를 자동 추정하지 않습니다. SSH 대상은 /etc/nv_tegra_release 확인을 통과해야 합니다.</p>
<label for="sshTarget" class="label">SSH 대상</label><div class="actions">
<input id="sshTarget" placeholder="SSH 별칭 또는 사용자@호스트" maxlength="128" autocomplete="off">
<button id="saveTarget">SSH 대상 저장</button></div></section>
<section class="card section"><h2>확인 결과</h2><pre id="result" aria-live="polite">버튼을 누르면 확인 결과가 표시됩니다.</pre></section>
</main><script>
const $=id=>document.getElementById(id);
const stateLabel=(v,connected)=>!connected?'미연결':v===true?'정상':v===false?'대기':'확인 불가';
const ageLabel=v=>Number.isFinite(v)?v.toFixed(2)+'초 전':'확인 불가';
const stageLabel={OK:'정상',WAITING:'대기',DISCONNECTED:'끊김',ERROR:'오류'};
let token='',busy=false,refreshing=false,configured=false;
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    const response=await fetch('/api/jetson/status',{cache:'no-store'});
    if(!response.ok)throw new Error('상태 API 응답 '+response.status);
    const d=await response.json(),s=d.status||{},jl=d.jetlink||{},local=d.local_jetlink||{},live=d.connected===true;
    const waiting=local.waiting_modeld===true,enabledWaiting=local.enabled===true&&!live;
    token=d.csrf||'';
    $('hero').className='hero '+(d.state==='오류'?'error':live?'online':enabledWaiting?'waiting':'');
    $('stateText').textContent=live?d.state+' · Jetson 연결됨':waiting?'대기 · Jetlink 활성화됨':enabledWaiting?d.state+' · Jetlink 연결 대기':d.state+' · Jetson 연결 확인';
    $('diagnosis').textContent=d.diagnosis+(d.receiver_error?' · '+d.receiver_error:'');
    $('stages').replaceChildren();
    for(const stage of d.stages||[]){
      const box=document.createElement('div'),label=document.createElement('div'),state=document.createElement('strong'),detail=document.createElement('div');
      box.className='stage';box.dataset.state=stage.state;label.textContent=stage.label;
      state.textContent=stageLabel[stage.state]||'확인 불가';detail.className='sub';detail.textContent=stage.detail||'';
      box.append(label,state,detail);$('stages').append(box);
    }
    const recent=d.recent||{},health=d.health||{},p=d.preview||{};
    $('recentStages').textContent=waiting?'오프로드 대기 · modeld 시작 후 heartbeat · 영상 · 추론 · HUD 확인':
      'Heartbeat '+ageLabel(recent.heartbeat_age_s)+' · 영상 '+ageLabel(recent.camera_frame_age_s)
      +' · 추론 '+ageLabel(recent.inference_age_s)+' · HUD TX '+ageLabel(recent.hud_tx_age_s);
    $('negotiation').textContent=waiting?'Jetlink 활성화됨 · modeld 시작 시 기능 협상 시작':
      d.capability_negotiated?'Jetlink 기능 협상 완료':'Jetlink 기능 협상 대기 또는 legacy 경로';
    $('temperature').textContent=(Number.isFinite(health.temperature_c)?health.temperature_c.toFixed(1)+'℃':'확인 불가')+' · '+(health.thermal||'unknown');
    $('interface').textContent=waiting?(local.usb_carrier===true?'USB · usb0 carrier 1 · peer 응답 대기':'USB · peer 응답 대기'):
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
      $('usb').textContent=local.usb_carrier===true?'물리 연결됨 · 응답 대기':local.usb_carrier===false?'물리 연결 확인 필요':'carrier 확인 대기';
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
    $('error').textContent=s.last_error||(local.conflict===true?(local.summary||'Jetlink 설정 충돌'):'보고된 오류 없음');
    $('webOpen').hidden=!jl.reachable;if(jl.reachable)$('webOpen').href=jl.url;
    for(const id of ['restart','reconnect'])$(id).disabled=!d.can_restart||busy||d.management_busy;
    $('restartHint').textContent=waiting?'오프로드 · modeld 시작 대기 중이라 Jetson 서비스 제어가 필요하지 않습니다.':
      d.can_restart?'정차 확인됨 · 재시작 전 확인 창이 표시됩니다.':'주행 중 또는 상태 확인 불가 · 재시작 사용 불가';
    if(!configured){$('sshTarget').value=d.ssh_target||'';configured=true;}
  }catch(error){
    $('hero').className='hero error';$('stateText').textContent='상태 확인 오류';$('diagnosis').textContent=String(error);
    for(const id of ['ip','comma','usb','usb3','service','frame','model','ready','age','pipeline','web'])$(id).textContent='확인 불가';
    for(const id of ['stages','recentStages','negotiation','temperature','interface','storage','version','previewMetrics'])$(id).textContent='확인 불가';
    $('restart').disabled=$('reconnect').disabled=true;$('webOpen').hidden=true;
  }finally{refreshing=false;}
}
async function action(name,extra={}){
  if(busy)return;
  const destructive=['restart','reconnect'].includes(name);
  if(destructive&&!confirm('Jetson 서비스를 재시작하면 영상·AI 연결이 잠시 끊깁니다. 안전하게 주차하고 진행할까요?'))return;
  busy=true;document.querySelectorAll('button[data-action]').forEach(button=>button.disabled=true);
  $('result').textContent='확인 중…';
  try{
    const response=await fetch('/api/jetson/action',{method:'POST',headers:{'Content-Type':'application/json','X-Jetson-Token':token},
      body:JSON.stringify({action:name,confirm:destructive,...extra})});
    const text=await response.text();let value;try{value=JSON.parse(text);}catch{throw new Error(text);}
    $('result').textContent=JSON.stringify(value,null,2);
  }catch(error){$('result').textContent='확인 실패: '+String(error);}
  finally{busy=false;document.querySelectorAll('button[data-action]').forEach(button=>button.disabled=false);await refresh();}
}
$('refresh').onclick=refresh;
document.querySelectorAll('button[data-action]').forEach(button=>button.onclick=()=>action(button.dataset.action));
$('saveTarget').onclick=()=>action('configure',{ssh_target:$('sshTarget').value.trim()});
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
