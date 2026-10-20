# NEXO Jetson 연결 복구 및 당근 최신 변경 검토 — 2026-10-20

## 작업 기준

| 저장소·브랜치 | 작업 시작 시 원격 HEAD |
|---|---|
| fallen8angel/NEXOdriveXPlus `NEXO` | `c40bce87035a15296666451187f4d867ec0ca477` |
| ajouatom/openpilot `carrot-wip` | `6ab4f7ff23b2f15f18fc7cfd2f9e5b0a93257876` |
| ajouatom/openpilot `carrot-nav` | `5a0e8158c269e07f05ac40ac59d043d740941ca6` |
| ajouatom/carrot-jetson `main` | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` |
| zoompilot/jetlink `main` | `d24328d849b0e5154b55f149dc18c5dfe8bd79cc` |

`git ls-remote`로 다시 확인했다. 요청한 wip HEAD와 일치하며 이전 기준
`ba186915179816a9b0476cec52bf321e73d2d4b0` 이후에는 아래 네 커밋만 있다.
NEXO HEAD는 [PR #28](https://github.com/fallen8angel/NEXOdriveXPlus/pull/28)의 병합 커밋이다.
PR의 제목·본문·병합 상태와 병합된 실제 코드를 확인했다. NCM-TCP를 새로 구현하거나
이전 Jetlink 브랜치를 다시 병합하지 않았다.

작업 브랜치는 `codex/jetson-recovery-20261020`이며 NEXO 본 브랜치에 직접 푸시하지 않는다.
장치 접속·설치·설정 변경·재부팅·주행 시험은 수행하지 않았다.

## 네 커밋의 실제 의미와 선별 결과

| 커밋 | 변경 파일·동작 | NEXO 대조 및 적용 판단 |
|---|---|---|
| [4abe6edc](https://github.com/ajouatom/openpilot/commit/4abe6edcb5b423116aba67fdf90058b0ce9d1e5d) | 44개 파일. S3 Wi-Fi display/protocol, 펌웨어·프로비저닝·웹 설치/다운로드 경로와 관련 테스트 제거. `cluster/main.py`가 factory 대신 `TuringUsbDisplay`를 직접 생성하고, `cluster_autorun.py`의 S3 발견/codec/대기 분기를 제거. H.264 오류 문구와 설정 설명 정리. 실제 기능 삭제 및 실행 경로 변경이다. | NEXO renderer는 이미 `TuringUsbDisplay`를 직접 생성하며 해당 S3 factory/설치 트리가 없다. Jetson `hud.py`가 panel 검색·단일 잠금·재실행을 이미 담당한다. 대량 삭제를 복사할 필요가 없다. 공유 USB 대기 정책 삭제나 인코더 설정 변경도 하지 않았다. |
| [2c076bc8](https://github.com/ajouatom/openpilot/commit/2c076bc8) | `AGENTS.md`, 신규 `docs/jetson_wifi_usb_panel_20261020.md`, 한·영 settings 문서, `carrot_settings.json` 5개. S3 폐기, nav 전용 Wi-Fi runtime과 wip 구분·배포 준비 기록 및 설명 문구 변경. Python 통신/서비스 구현은 없다. | 이 커밋 자체를 젯슨 통신 개선으로 가져올 수 없다. NEXO의 USB v2 runtime과 모델 고정을 유지했다. 당근의 타 장치 배포 승인·사설 환경 지침은 NEXO 작업에 적용하지 않았다. |
| [a28734cd](https://github.com/ajouatom/openpilot/commit/a28734cd423c1356bcb8365d0fa79ec49915b504) | `openpilot/selfdrive/carrot/tests/test_cluster_scene.py` 1개. 두 geometry 테스트에서 `signal_display.display_requested=False`를 명시해 native Params 의존성을 격리. 테스트만 바뀌며 HUD 표시 동작은 동일하다. | NEXO의 `cluster_renderer.py`에는 해당 signal_display/display_requested 경로가 없다. 그대로 복사하면 없는 모듈 의존성이 생긴다. 새 회귀 테스트에서는 기존 AST 함수 실행/소형 대역 방식으로 Linux native 의존성을 격리했다. |
| [6ab4f7ff](https://github.com/ajouatom/openpilot/commit/6ab4f7ff23b2f15f18fc7cfd2f9e5b0a93257876) | `AGENTS.md`, `docs/jetson_wifi_usb_panel_20261020.md` 2개. nav의 실제 AP 시작·서명 runtime 활성화·USB panel 송신 관측을 추가한 기록이다. 통신 코드 변경은 없다. | 하드웨어 사례는 참고 자료다. NEXO/넥쏘에서 시험한 결과로 간주하지 않는다. 마지막 60초 model/DM은 각각 약 19.998Hz, 지도 bytes는 0이며 실물 화면 독립 확인·RF 복구·장시간 주행 검증은 기록상 완료되지 않았다. |

부록에 각 커밋의 모든 변경 경로를 기재했다. 네 커밋을 cherry-pick하지 않고
기존 코드에서 재현한 복구 결함만 수정했다.

## 연결 구조: 당근과 NEXO

```text
당근 carrot-wip: comma ── Jetlink USB 모델/상태/표시 ── Jetson ── USB ── TURZX
당근 carrot-nav: comma ── 기존 USB 모델/프로비저닝 ── Jetson
                    └── 전용 Wi-Fi HUD/내비 ───────────┘ ── USB ── TURZX
NEXO NCM opt-in: comma g1 (ADB + ncm.0)
                    └── USB-NCM / JLNK v2 TCP ──────── Jetson ── USB ── TURZX
```

### 당근의 USB와 Wi-Fi 역할

기존 USB bulk 및 auto/mobile composite 구성은 comma 쪽에서 자신이 소유한
FunctionFS gadget을 사용한다. composite setup 역시 먼저 gadget helper를 호출하며,
다른 gadget이 유일한 UDC를 점유하면 거부한다. 따라서 당근의 최근 하드웨어 성공 기록이
AGNOS `g1`·ADB를 보존하는 NEXO 구성의 성공을 입증하지는 않는다.

nav 소스의 `tools/jetlink/wifi_direct.py`, `wifi_direct_netlink.py`는 실제 AP+station
동시 지원을 확인하고 **offroad에서만** 전용 AP를 시작한다. Park 자체는 offroad가 아니다.
USB로 AP profile과 임시 HUD 키를 전달하고, `hud_wifi.py`/`hud_wifi_worker.py`의 별도
프로세스가 snapshot/media TCP를 상호 인증·암호화한다. `hud_wifi_route.py`는 WLAN
직접 경로를 확인하고 전용 AP를 우선한다. 일반 인터넷용 wlan0와 HUD AP의 역할을 구분한다.

nav daemon의 `usb_display` 조건은 Jetson을 제외한다. Jetson receiver도 USB HUD를
거부하므로 WLAN 실패 시 USB HUD fallback이 없다. USB 모델 추론은 계속 별도로 사용한다.
이 방식은 HUD 대기를 추론 통신 밖으로 옮기는 장점이 있지만, AP 권한/드라이버 동시 모드,
RF 상태, 양쪽 runtime capability와 서명 배포가 필요하다. wip의 네 최신 커밋에 이
통신 구현이 포함된 것은 아니다. 최신 독립 Jetlink는 v3이고 NEXO/pinned 서버는 v2여서
최신 transport나 nav runtime을 그대로 바꿔 넣을 수 없다.

### NEXO가 이미 갖춘 기능과 복구 조건

- PR #28의 NCM-TCP는 기존 `g1`의 NCM 네트워크를 사용한다. 별도 gadget을 만들거나
  `g1`을 unbind/delete하지 않는다. 기본 bulk와 명시적 NCM opt-in의 선택도 유지한다.
- 양쪽 고정 주소, comma의 활성 gadget NCM 및 Jetson의 `cdc_ncm`, 직접 route를 검사하고
  `SO_BINDTODEVICE`로 socket을 해당 interface에 고정한다. Wi-Fi 우회는 없다.
- comma는 모델 SHA·계약 및 warmup을 확인한 뒤 ready가 된다. 영상 입력과 모델 context를
  INFER로, 차량 상태·NEXO 주차 정보·설정·카메라 preview를 HUD snapshot으로,
  내비 media를 별도 bounded 메시지로 전달한다. HUD로 차량 제어 메시지를 발행하지 않는다.
- 기존 YOLO/frame bridge는 roadEncodeData 구독·세대 변경·keyframe 대기를 사용한다.
  HUD와 native inference 통신은 이 YOLO 프로그램을 대체하지 않는다.
- 설치된 systemd server/HUD unit은 부팅 시 시작하고 2초 restart 간격을 사용한다.
  renderer는 기존 `hud.lock`, 실제 VID/PID `1cbe:0092/0123` 검색, snapshot/onroad 또는
  debug 조건과 serial recovery loop를 사용한다. panel 재연결 시 중복 owner를 생성하지 않는다.
- 재부팅 뒤 **설정한 interface 이름·주소·직접 route가 복원되어야** 자동 세션 재연결이 가능하다.
  이번 코드는 주소/DHCP/NetworkManager profile을 생성하지 않는다. 재부팅 후 주소가 사라지는
  현장 문제는 이 변경만으로 해결됐다고 말할 수 없다.

## 실제 구현한 개선

### 1. NCM USB 재열거와 늦은 네트워크 준비

`openpilot/tools/jetson/ncm_server.py`의 기존 listener는 생성 당시 interface에 계속
묶여 있었다. 같은 이름의 NIC가 새 ifindex로 재생성되면 새 USB로 접속을 받을 수 없는데도
accept timeout을 반복할 수 있었다. 초기 주소/route 미준비도 opener 생성 예외로 이어졌다.

이제 listener를 필요 시 만들고 `if_nametoindex`로 소실/교체를 확인한다. 바인딩 중과 peer
수락 전에도 index를 확인한다. 소실·주소/route·bind 실패 시 listener/미검증 peer만 닫고
기존 pinned server `_serve`의 2초 대기로 돌아간다. 다시 준비되면 전체 topology와
source/interface 제약을 재검사한 새 listener를 만든다. 동일 오류 로그는 억제한다.
`native_host.py`는 서버 종료 시 만들어진 opener를 명시적으로 닫는다.

`_serve`의 공유 EngineHost와 preload는 loop 밖에 있다. 네트워크 실패를 None으로
반환하므로 그 실패 때문에 서버 프로세스를 재시작하거나 TensorRT 엔진을 반복 로드하지
않는 구조다. 실제 GPU 메모리/복구 시간 측정은 수행하지 않았다.

### 2. 로컬 modeld socket이 열린 유휴 상태의 연결 확인

`openpilot/selfdrive/modeld/jetlink/daemon.py`는 listener accept 대기에서만 STATE를
조회하고, 이미 연결된 local modeld가 frame을 보내지 않으면 ready를 반복 게시했다.
이제 두 idle timeout 경로에서 마지막 telemetry 이후 1초 이상일 때만 STATE를 조회한다.
실패는 기존 cleanup/retrying 경로로 전달되고 새 세션에서 다시 계약·warmup을 수행한다.
연결된 유휴 socket의 끊김 감지 누락을 고치고, 미연결 idle에서는 통상 초당 4회였던 조회를
최대 초당 1회로 줄인다. 활성 추론의 매 frame 앞에 별도 STATE를 넣지 않는다.

STATE 한 회의 기존 0.5초 timeout은 유지한다. 유휴 중 새 frame이 도착하는 경쟁 상황의
대기와 실물 지연 분포는 추가 측정 대상이다. 원격 추론 active 실패 시 기존 modeld restart
정책도 유지하므로 무중단 로컬 전환을 보장하지 않는다.

### 3. HUD snapshot 만료 시 정리·재시작

`openpilot/tools/jetson/hud.py`의 기존 panel wrapper는 snapshot이 None이어도 H.264
송신을 계속 허용했다. 이제 매 송신 전 신선한 snapshot 하나로 상태를 확인하고, 만료되면
세션을 종료해 renderer/encoder 정리 및 status marker 만료를 거친다. 기존 잠금과 backoff
supervisor가 새 snapshot/panel을 기다렸다가 동일 프로세스에서 다시 시작한다.
offroad 정상 종료도 유지한다. 준비 단계에서는 live snapshot이 HUD를 요청하지 않으면
USB 열거를 생략하고, 한 송신 조건에서 snapshot을 반복 읽던 부분을 한 번으로 줄였다.

이 변경은 오래된 차량 화면 송신과 불필요한 rendering을 중단하는 것이며 panel 전원을
강제로 끄거나 실물 framebuffer의 잔상을 없앤다는 보장은 아니다. 화면 구성·FPS·codec·
밝기·YOLO/DM/주행 모델·차량 제어 정책은 변경하지 않았다.

### 4. comma 진단에서 전송 방식 구분

기존 `/api/jetson/status`의 `native_link.transport`와 통합 TXT 진단에
`USB bulk` 또는 `USB-NCM/TCP (existing g1)`를 표시한다. retrying 상태에도 확인할 수 있고
상태 lease 만료 시 unknown으로 바뀐다. 단순 물리 USB 연결, 모델 ready/active와 HUD 송신·
수신 증거의 기존 구분은 유지한다. 새 daemon, 포트, 원격 제어 API를 만들지 않았다.

## 동시 부하와 적용하지 않은 변경

기존 AI 결과 우선 반환, 그 뒤 최대 12ms display tail, 낮은 우선순위 producer,
bounded media/read-ahead 큐를 유지했다. NCM/TURZX와 CPU/GPU·메모리·USB controller는
여전히 공유 자원이다. libx264 HUD encoding과 YOLO/TensorRT 동시 부하는 실제 Jetson에서
end-to-end P50/P95/P99/최대 지연·메모리·온도·model/DM 연속성을 측정해야 한다.
위 12ms는 부하가 없다는 뜻도, 다음 frame 지연이 0이라는 뜻도 아니다.

Wi-Fi HUD/AP 전환, 새로운 protocol v3, 당근 모델/서명 runtime 교체, g1 gadget 합성/해제,
USB 전류 차단/role 변경, 카메라나 YOLO 중복 실행, 모델 deadline 변경은 적용하지 않았다.
NEXO에 없는 S3 트리 삭제와 적용 대상이 다른 테스트 patch도 가져오지 않았다.

## 검증

- 수정 전 NEXO production 파일에 새 idle/listener 회귀 assertion을 실행하면 **5개 실패**한다.
  연결된 local socket의 끊김 미감지 1개, idle 조회 간격 2개, 초기 주소 미준비 1개,
  대기 중 NIC 소실 미처리 1개를 분리해 확인했다. 이후 수정 파일을 원래 작업 내용으로 복원했다.
- 관련 최종 회귀: **286 passed, 4 skipped, 1 deselected**. Windows Python 3.12,
  작업용 가상 환경과 실제 pinned carrot-jetson checkout을 사용했다.
- 실제 TCP/socketpair framing·정체/deadline·EOF·padding·손상된 메시지, pinned Session/
  PolicyQueues/ReadAheadTransport, 실제 NEXO ONNX 계약과 모의 GPU의 추론→HUD→추론,
  기존 USB/NCM·설치/관리·웹 API·CSRF·HUD 종료·재연결 회귀를 포함한다.
- 신규 검사는 idle telemetry throttle/실패 전달, 초기 주소 미준비·NIC 소실/동일 이름 새 index·
  짧은 탈착 사이의 index 변경·route 상실 시 미검증 peer 정리·중복 오류 억제,
  실제 panel wrapper와 serial supervisor에서 snapshot 만료 후 송신 중단/재개,
  offroad 종료 및 전송 방식 진단 만료를 포함한다. Linux sysfs/NIC·GPU·panel은 대역이다.
- 4개 skip: pyray 1, built Linux cereal/msgq 2, Linux flock 1.
  기존 Windows directory rename rollback 1개는 PR #28과 동일하게 제외했다.
  pytest C++ plugin 설정 및 기존 aiohttp 관련 경고 19개가 남아 있다.
- 변경 Python 파일 Ruff 및 Python 3.10 grammar, `git diff --check`를 확인했다.
  기존 관리 테스트의 무시간 accept 대역은 새 throttle에서 1초 busy loop와 게시 횟수 경합을
  일으켜 최종 재실행에서 한 번 실패했다. 대역 accept가 가상 시간을 0.25초 전진하도록
  고쳐 실제 대기를 모델링했으며, 실패 즉시 세션 종료라는 assertion은 유지했다.
  초기 sandbox 실행의 Temp 디렉터리 권한 및 loopback socket 제한, 누락한 테스트 의존성은
  작업용 temp/venv와 허용된 loopback 실행으로 해결했다. 이 초기 실행은 제품 실패가 아니다.

재현 명령(저장소 root, 테스트 의존성 설치 및 CARROT_JETSON 설정 후):

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONUTF8=1 CARROT_JETSON=<d0dc96fa checkout>
python -m pytest -p pytest_asyncio.plugin -o asyncio_mode=auto -o addopts='' --noconftest \
  --basetemp=<작업용 임시 디렉터리> \
  openpilot/selfdrive/modeld/tests/test_jetlink_ncm_transport.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_ncm_server.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_network.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_display.py \
  openpilot/tools/jetson/tests \
  openpilot/selfdrive/carrot/server/tests/test_jetson_local_status.py \
  -q --tb=short -rs -k 'not test_service_start_failure_rolls_back_after_candidate_selection'
```

## 실제 장치에 남은 확인 사항

1. 별도 브랜치를 양쪽에서 검토 배포하고, PR #28의 opt-in 설치값 및 동일 ONNX SHA를 확인한다.
   기존 설치 파일은 GitHub push만으로 갱신되지 않는다. 이번 작업에서 서비스는 설치하지 않았다.
2. g1/ADB 유지, cdc_ncm interface 이름 및 양쪽 주소·직접 route가 재부팅 후 복원되는지 확인한다.
   읽기 전용 `diagnose_usb_owner.py --role comma|jetson`과 같은 시간대 kernel/service 로그를 수집한다.
3. 정차 상태에서 케이블 탈착, Jetson 늦은 부팅/재부팅, 서비스 재시작, 주소 상실/복원,
   TURZX 탈착을 시험한다. retrying→ready, HUD marker와 실제 화면을 각각 확인한다.
4. YOLO+추론+HUD 동시 부하와 장시간 온도·메모리·지연/연속성을 확인한다.
5. 실패 시 기존 offroad disable 절차로 로컬 모델 경로를 선택한다. g1을 해제하지 않는다.

이번 결과는 소프트웨어 회귀 검증이다. 실제 Linux SO_BINDTODEVICE/권한/USB ifindex 변화,
NCM 주소 지속성, TensorRT 출력·성능, panel 화질·재연결과 실차 주행은 미검증이다.

Docs-Not-Needed: 새 설정이나 사용자 가이드 변경 없이 기존 선택 통신 경로의 복구와 진단을 보완했으며, 요청한 분석과 검증 범위를 이 개발 문서에 기록했다.

## 참고 소스

- [PR #28과 기존 상세 검토](https://github.com/fallen8angel/NEXOdriveXPlus/pull/28)
- [wip의 하드웨어 기록](https://github.com/ajouatom/openpilot/blob/6ab4f7ff23b2f15f18fc7cfd2f9e5b0a93257876/docs/jetson_wifi_usb_panel_20261020.md)
- [nav Wi-Fi 설계 및 통합 구분](https://github.com/ajouatom/openpilot/blob/5a0e8158c269e07f05ac40ac59d043d740941ca6/docs/jetson_wifi_review.md)
- [nav 실제 daemon](https://github.com/ajouatom/openpilot/blob/5a0e8158c269e07f05ac40ac59d043d740941ca6/openpilot/selfdrive/modeld/jetlink/daemon.py)
- [pinned 서버 세션 loop](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/third_party/jetlink/jetlink/server/main.py)
- [독립 Jetlink 기준](https://github.com/zoompilot/jetlink/tree/d24328d849b0e5154b55f149dc18c5dfe8bd79cc)


## 부록: upstream 커밋별 전체 변경 파일

M=수정, A=추가, D=삭제. 경로는 당근 저장소 기준이다.


### 4abe6edcb5b423116aba67fdf90058b0ce9d1e5d

```text
M	docs/user/en/settings.md
M	docs/user/ko/settings.md
M	openpilot/selfdrive/carrot/cluster/cluster_h264_pipeline.py
D	openpilot/selfdrive/carrot/cluster/cluster_wifi_display.py
D	openpilot/selfdrive/carrot/cluster/cluster_wifi_protocol.py
M	openpilot/selfdrive/carrot/cluster/main.py
M	openpilot/selfdrive/carrot/cluster_autorun.py
M	openpilot/selfdrive/carrot/tests/test_cluster_autorun.py
M	openpilot/selfdrive/carrot/tests/test_cluster_gc_lifecycle.py
M	openpilot/selfdrive/carrot/tests/test_cluster_h264_complexity.py
M	openpilot/selfdrive/carrot/tests/test_cluster_retry_logging.py
D	openpilot/selfdrive/carrot/tests/test_cluster_wifi.py
D	openpilot/selfdrive/carrot/tests/test_cluster_wifi_h264.py
M	openpilot/selfdrive/carrot_settings.json
M	tools/carrot_route_vault/tests/test_viewer.py
M	tools/carrot_route_vault/viewer.py
D	tools/esp32s3_display/INSTALL_KO.md
D	tools/esp32s3_display/README.md
D	tools/esp32s3_display/firmware/.gitignore
D	tools/esp32s3_display/firmware/CMakeLists.txt
D	tools/esp32s3_display/firmware/main/CMakeLists.txt
D	tools/esp32s3_display/firmware/main/Kconfig.projbuild
D	tools/esp32s3_display/firmware/main/display_status.c
D	tools/esp32s3_display/firmware/main/display_status.h
D	tools/esp32s3_display/firmware/main/network.c
D	tools/esp32s3_display/firmware/main/network.h
D	tools/esp32s3_display/firmware/main/provision.c
D	tools/esp32s3_display/firmware/main/provision.h
D	tools/esp32s3_display/firmware/main/status_led.c
D	tools/esp32s3_display/firmware/main/status_led.h
D	tools/esp32s3_display/firmware/main/status_led_policy.h
D	tools/esp32s3_display/firmware/main/usb_bridge.c
D	tools/esp32s3_display/firmware/main/wifi_recovery_policy.h
D	tools/esp32s3_display/firmware/partitions.csv
D	tools/esp32s3_display/firmware/sdkconfig.defaults
D	tools/esp32s3_display/installer/index.html
D	tools/esp32s3_display/installer/installer.js
D	tools/esp32s3_display/installer/manifest.mjs
D	tools/esp32s3_display/installer/tests/install_flow.test.mjs
D	tools/esp32s3_display/installer/tests/manifest.test.mjs
D	tools/esp32s3_display/package_release.py
D	tools/esp32s3_display/provision.py
D	tools/esp32s3_display/tests/test_status_led.c
D	tools/esp32s3_display/tests/test_wifi_recovery.c
```


### 2c076bc8f8971849333656a7f5a71453a4a12aa4

```text
M	AGENTS.md
A	docs/jetson_wifi_usb_panel_20261020.md
M	docs/user/en/settings.md
M	docs/user/ko/settings.md
M	openpilot/selfdrive/carrot_settings.json
```


### a28734cd423c1356bcb8365d0fa79ec49915b504

```text
M	openpilot/selfdrive/carrot/tests/test_cluster_scene.py
```


### 6ab4f7ff23b2f15f18fc7cfd2f9e5b0a93257876

```text
M	AGENTS.md
M	docs/jetson_wifi_usb_panel_20261020.md
```
