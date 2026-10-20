# NEXO Jetson USB·상태·카메라 진단 보완 — 2026-10-20

## 완료 범위

코드 수정 및 소프트웨어 회귀 검증을 수행했다. 실제 comma4/Jetson/TURZX에는 접속하거나 설치하지 않았다.
USB 연결 성공, 실제 TensorRT 출력·지연, 실차 안전성은 **미검증**이다.
기본 transport는 기존 `usb`이며 이번 패치가 자동으로 NCM을 활성화하거나 IP를 설정하지 않는다.
따라서 차량이 계속 bulk 모드를 선택한다면 `g1` 충돌은 계속 ERROR다.

## 최신 HEAD와 기존 작업

GitHub API로 작업 시작 때 네 저장소 HEAD를 다시 확인했다. 사용자가 제시한 두 기준과 일치했다.

| 저장소·브랜치 | 확인한 SHA |
|---|---|
| NEXOdriveXPlus / NEXO | `a5add8d63de5aaaef1dd4c8a8af0945adaf927dd` |
| ajouatom/openpilot / carrot-wip | `ca5c6be2089d8f1248794801c3b53a2e4b4125fe` |
| ajouatom/carrot-jetson / HEAD | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` |
| zoompilot/jetlink / HEAD | `d24328d849b0e5154b55f149dc18c5dfe8bd79cc` |

PR #28의 g1 보존 NCM-TCP와 현재 HEAD의 리스너 ifindex 복구, idle STATE 조회,
HUD snapshot 만료, YOLO/direct 영상·renderer 단일 잠금 기능을 그대로 재사용했다.
이전 `codex/jetson-recovery-20261020` 브랜치는 현재 NEXO와 같은 SHA다.
미병합 PR #27은 현재 NEXO와 6 ahead/14 behind로 갈라졌으며, 실제 파일의 고정 usb0 사전 점검과
독립 FunctionFS 충돌 분류가 중심이다. 그 브랜치를 재사용하면 이미 반영된 NCM 개선을 다시 통합해야 한다.
PR #19의 포트 충돌 대응 의도도 확인했고, 현재 코드의 공유 수신기·만료 처리와 함께 보완했다.
따라서 최신 NEXO에서 `codex/jetson-usb-status-gap-20261020` 브랜치를 만들었다.
NEXO 직접 수정·push·병합 또는 차량의 로컬 수정 초기화는 하지 않는다.

## 당근과의 차이 및 적용 판단

이전 NEXO 검토 이후 carrot-wip에는 다음 5개 커밋이 있다.

| 커밋 | 실제 변경 | 이번 적용 |
|---|---|---|
| `8c5b3173` | nav N16 배포 기록·관련 문서 | NEXO 모델이나 nav runtime을 가져오지 않음 |
| `8cf59711` | Git Pull/재부팅 업데이트 정책 문서 | 차량 41개 로컬 수정 보존이 우선이므로 그대로 적용하지 않음 |
| `5535fbb0` | DATA 용량·NAV 표시 주행 진단 기록 | 타 차량의 관측을 NEXO 성공 증거로 사용하지 않음 |
| `10d48e66` | 초기 연결 예외 분류, 한글 배지, CameraGapTrace 및 테스트 | 개념을 NEXO 모델/transport 구조에 맞게 구현 |
| `ca5c6be2` | startup 배지 통합 검증 기록 | 실제 NEXO 회귀를 별도로 실행 |

[핵심 당근 커밋](https://github.com/ajouatom/openpilot/commit/10d48e66a409230b4f55c68b1c4b0f01f6ffbd6b)은
첫 handshake timeout/no-reader만 120초 안에서 대기로 처리하고, 모델·프로토콜·실행 오류는 유지한다.
당근 boot-gate RuntimeHandoff 경로는 NEXO에 없으므로 만들지 않았다.
NEXO에서는 TCP 서버의 초기 connection refusal도 같은 한정된 준비 대기로 분류한다.
정상 세션을 한 번이라도 맺은 뒤의 timeout은 다시 초기 대기로 숨기지 않는다.

당근의 현재 카메라 pairing 함수와 NEXO의 루프는 다르다. NEXO는 기존 main/extra 수신 루프,
25ms 전진 조건과 10ms skew 경고, prepare_only·누락 계산을 유지한다. 당근 pairing 정책을 복사하지 않았다.
독립 Jetlink 최신판은 v3지만 NEXO/pinned carrot-jetson은 v2다. protocol/서버 runtime을 교체하지 않았다.
이전 당근 HUD 전송 개선과 NEXO의 inference-first·bounded HUD tail은 이미 반영돼 있어 재개발하지 않았다.

## 확인한 결함과 실제 수정

### USB-NCM

- 콤마 client는 `USB_IFACE=usb0`에 고정돼 있었다. 이제 bound gadget의 실제 NCM `ifname`을
  매 연결 때 찾아 하나인 경우만 사용한다. 없거나 둘 이상이면 추측하지 않고 오류를 남긴다.
- admin-UP/주소/route만으로는 carrier가 끊긴 NIC도 통과했다. 실제 sysfs carrier=1을 요구하고,
  읽기 실패·0·빈 값은 구체적인 오류로 거부한다. 변경 전 함수로 새 회귀를 실행하면 3건이 실패한다.
- connect 전후 ifindex/활성 ifname/주소·route를 재검사한다. 교체 중인 socket은 닫고 기존 retry로 돌아간다.
- `SO_BINDTODEVICE`, source bind, gateway 없는 직접 route, 기존 고정 주소쌍을 유지한다.
  Wi-Fi/기본 경로 우회나 g1/ADB 해제는 없다. 권한 오류에는 실제 OS 오류를 함께 남긴다.
- 기존 offroad interface-UP helper와 7000 페이지도 NCM 선택 시 같은 실제 이름을 쓴다.
  링크 UP 작업은 기존 offroad 조건을 다시 확인한다. 주소·route 생성은 하지 않는다.
- Jetson 리스너는 설치 당시 interface가 사라졌을 때만 대체 NIC를 찾는다. 이미 `.250/24`, carrier,
  `.127` 직접 route를 갖춘 유일한 `cdc_ncm`이어야 한다. 설치 interface가 아직 존재하면 다른 장치로
  바꾸지 않는다. 새 listener/accepted socket에 다시 bind하고 EngineHost나 HUD/YOLO 서비스는 재시작하지 않는다.
  NIC 이름만 바뀌고 주소가 복원되지 않았다면 계속 오류/대기하며, 주소를 임의로 붙이지 않는다.

이 보완이 원래의 g1 충돌을 bulk에서 없애는 것은 아니다. g1을 보존하려면 양쪽 네트워크를 확인한 뒤
기존 `enable-ncm-tcp`와 Jetson TCP 설치를 명시적으로 선택해야 한다.

### 준비·READY·실제 오류

`waiting_model_contract → connecting/hello → loading/model → ready` 단계를 보고한다.
HELLO/엔진 대기 중 1초 간격 상태 갱신을 하되 기존 전체 준비 제한과 frame deadline은 유지한다.
첫 연결의 timeout/refusal/no-reader만 120초 안에서 `wait_reason`을 가진 connecting으로 표시한다.
g1 충돌, 주소·carrier·직접 route·권한 오류, 잘못된 프로토콜, 모델 검증 실패, 준비 후 통신 단절은 ERROR다.

콤마의 기존 NativeJetlinkStatus/JetsonBadge를 재사용한다. 연결 대기·통신 준비·모델 준비는 노랑,
USB/모델 오류·재연결 오류는 빨강, READY/추론 활성화는 기존 표시다.
미시의 연결 아이콘은 준비 배지가 있다는 이유만으로 켜지지 않는다.
READY/active는 신선한 peer 증거가 있어야 하며, active는 같은 SHA의 신선한 model 상태도 요구한다.
이는 표시용 정보이며 selfdrived 차량 경고·engagement 허용·제어 메시지와 연결하지 않았다.

7000 `/api/jetson/status`에 native stage/label/severity/wait_reason/interface를 노출한다.
YOLO의 Wi-Fi 비콘이 살아 있어도 native USB/모델 오류를 WAITING이나 정상으로 덮지 않는다.
페이지는 실제 오류와 별도로 초기 대기 원인을 보여준다.

### 비콘 포트 충돌

현재 웹 서버와 8초 진단기가 모두 UDP 8766을 bind하는 코드 경로를 확인했고 실제 로컬 UDP로 재현했다.
차량 로그의 정확한 당시 PID 목록은 제공되지 않아 현장 점유 프로세스까지 확정한 것은 아니다.

8초 진단은 먼저 기존 7000 API의 관측을 읽는다. 웹 수신기가 있으면 별도 UDP listener를 만들지 않는다.
API가 없을 때만 exclusive fallback을 열며, 이미 점유됐고 API도 읽을 수 없으면 **진단 불가**로 표시한다.
0 packet/오프라인으로 잘못 단정하지 않는다. 기존 프로세스를 종료하거나 포트를 변경하지 않는다.
웹 수신기는 한 task만 유지하며 bind 실패 후 2초 간격 재시도로 복구한다.
기존 USB(localhost forwarding)·Wi-Fi lease와 8767/8768 등 다른 소비자는 유지한다.
채널별 packet 누계/수신 age/source를 제공하고, 오래된 비콘은 정상 상태를 연장하지 않는다.
fallback 진단의 age도 원격 wall-clock 대신 로컬 monotonic 수신 시간을 사용한다.

### CameraGapTrace

기존 recv를 한 번씩 감싸 프레임 ID·SOF/EOF·로컬 수신 시각·대기 시간을 관측한다.
한 사이클당 마지막 16개 수신 metadata와 총 수신 횟수, 직전/현재 사이클만 보존한다.
누락 ID는 first/last/count 구간으로 기록하므로 큰 프레임 번호 점프에서도 메모리가 늘어나지 않는다.
camera_wait/before_inference/inference/postprocess/loop 시간, backend, Jetson cached 연결 상태·ready·active,
prepare_only 및 오류를 함께 기록한다. model thread에서 네트워크나 추가 상태 파일을 조회하지 않는다.
상태는 기존 JoiningModel의 최대 1초 캐시이며 매 프레임의 실시간 peer probe가 아니다.
이벤트 이름은 `modelCameraGap`, 누락 때만 최대 1Hz다. 최초 attachment와 counter reset은 허위 누락으로 기록하지 않는다.
로그 실패를 삼키며 frame 선택·모델 입력·누락 복구·cameraOdometry 유효성은 바꾸지 않는다.
진단 추가이지 카메라 누락 원인의 해결 또는 locationd 수정은 아니다.

## 수정 파일

| 경로 (openpilot/ 기준) | 변경 |
|---|---|
| selfdrive/modeld/jetlink/ncm_transport.py | 실제 comma NCM 발견, carrier/ifindex, host rename 제한 탐색, 오류 상세 |
| tools/jetson/ncm_server.py | 같은 EngineHost 아래 새 host NIC로 listener/peer 재바인딩 |
| selfdrive/modeld/jetlink/network.py | 기존 offroad helper·관측에 실제 NCM 이름 사용 |
| selfdrive/modeld/jetlink/diagnose_usb_owner.py | 실제 comma NIC·주소·preflight 결과 진단 |
| selfdrive/modeld/jetlink/startup.py (신규) | 제한된 초기 대기 분류 |
| selfdrive/modeld/jetlink/client.py | HELLO/엔진 준비 상태 갱신 callback; frame 경로 기본 동작 유지 |
| selfdrive/modeld/jetlink/daemon.py | 준비 단계/진짜 오류·인터페이스 상태 보고 |
| common/jetson_status.py | 공통 배지 해석, freshness 및 오류 우선순위 |
| selfdrive/ui/jetson_badge.py, selfdrive/ui/mici/layouts/home.py | 진행/오류 색상, 준비 상태의 연결 아이콘 오인 방지 |
| selfdrive/carrot/server/services/jetson.py | 공통 native 단계, USB/모델 오류 노출 |
| selfdrive/carrot/server/features/jetson.py, jetson_pages.py | 단일 비콘 수신기 복구·관측 공유 및 상태 페이지 |
| selfdrive/carrot/server/features/tools/nexo_jetson_diag.py | 공유 수신기 사용, 충돌/수신 만료 분리 |
| selfdrive/modeld/camera_gap.py (신규), modeld_local.py, jetlink/model.py | bounded trace, 기존 모델/선택 정책의 관측 |
| selfdrive/modeld/jetlink/README.md, 이 문서 | 변경된 실제 interface 조건·적용 한계 |
| common/tests/test_jetson_status.py, selfdrive/ui/tests/test_jetson_badge.py(기존 재사용) | 배지·만료·아이콘 회귀 |
| selfdrive/modeld/tests/test_jetlink_ncm_transport.py, test_jetlink_ncm_server.py | NIC/carrier·실제 TCP 늦은 시작/서버 restart·host rename |
| selfdrive/modeld/tests/test_jetlink_startup.py, test_camera_gap.py (신규) | 대기 경계·실제 daemon 단계·불변 선택/진단 bounds |
| tools/jetson/tests/test_management.py, selfdrive/carrot/server/tests/test_jetson_diagnostic_receiver.py(신규) | 실제 UDP 충돌/자동 재bind·공유 관측·Wi-Fi와 native 오류 분리 |

## 보호 범위와 충돌 검토

CAN/DBC, Panda Safety, MED/SCC/FCA/크루즈, 종·횡제어, 레이더, 정지선, R/D,
주차센서 제어, 운전자 감시 설정, ONNX와 HUD layout/YOLO 실행 경로는 변경하지 않았다.
`modeld_local.py`와 `jetlink/model.py`는 새 진단 관측을 제거하면 기준과 AST가 동일하다.
기존 카메라 선택·입력·제어 계산, 원격 active 실패 후 modeld restart 정책을 유지했다.
`FRAME_TIMEOUT=0.15`와 로컬 IPC 제한도 유지한다. 무중단 로컬 복귀를 보장하지 않는다.

기존 HUD 단일 잠금, USB 재연결 backoff, snapshot 만료, YOLO 단일 실행·서비스 시작 제한을 재사용했다.
NCM 추론 socket은 TURZX 장치를 열지 않으며 HUD는 별도 VID/PID와 renderer를 사용한다.
실제 USB host controller·GPU·CPU·VRAM은 공유하므로 동시 부하 안정성은 실물 시험이 필요하다.
g1 UDC/ADB·USB 전원 role, N16 모델·v3 wire·다른 signed runtime은 변경하지 않았다.

모델 SHA는 `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`로 유지한다.
실제 ONNX의 2,576 float 출력, 393,216-byte 영상/2,096-byte context와 pinned v2 계약을 회귀에서 검증했다.
IP/interface 필터는 암호학적 인증이 아니며 신뢰된 전용 USB 링크라는 기존 전제를 유지한다.

## 테스트 결과와 재현

최종 결과: **373 passed, 4 skipped, 1 deselected**, 최종 실패 0. Windows Python 3.12.14.
실제 loopback TCP/UDP 및 pinned carrot-jetson 소스, 실제 ONNX metadata, 모의 GPU를 사용했다.
전체 openpilot/C++ 빌드 및 GitHub CI 성공을 주장하지 않는다.

| 요청 범주 | 결과 |
|---|---|
| 기존 단위/HUD/YOLO·설치·관리·웹 회귀 | 통과, 아래 플랫폼 제한 제외 |
| NCM 주소/route/driver/carrier·이름 변경·접속 중 교체 | 통과 (Linux topology는 대역) |
| USB 실패·늦은 Jetson/서버 준비·서버 restart | 통과 (실제 TCP + 가상 sysfs/daemon) |
| NEXO 모델 SHA·입출력 계약·추론→HUD→추론 | 통과 (GPU만 대역) |
| 초기 120초 경계·g1/모델/재연결 오류 우선 | 통과 |
| 실제 UDP 충돌, 단일 listener, 2초 재bind, 비콘 만료 | 통과 |
| CameraGapTrace bounds/누락 전후/로그 실패/원래 선택 루프 | 통과 |
| 실제 Linux SO_BINDTODEVICE/ADB·USB 탈착/지속 주소 | 미실행 |
| TensorRT 정확도·150ms 내 실제 지연·TURZX 화면·동시 부하·주행 | 미실행 |

skip 4: pyray 1, 빌드된 Linux cereal/msgq 2, Linux flock 1.
기존 Windows directory rename rollback 1건은 이전 PR과 동일하게 제외했다.
기존 pytest C++ 설정/aiohttp 경고 19개가 남는다.
새 fixture의 directory rename도 Windows에서 한 번 거부돼 실제 sysfs 소실/재생성 형태로 바꿨고,
미기동 TCP 서버가 Windows에서 refusal 또는 timeout을 줄 수 있다는 테스트 전제를 바로잡았다.
기존 verified-candidate 설치 시험에서도 directory rename이 한 번 실패했고, 짧은 새 임시 경로에서
전체 회귀를 다시 실행해 통과했다. 설치 production 코드는 수정하지 않았다.
제한된 socket sandbox에서 멈춘 초기 실행은 종료하고 실제 로컬 socket 허용 환경에서 검증했다.
기준 함수의 carrier 미검사 회귀 3건은 의도적으로 실패를 재현했고 수정본에서는 통과한다.

새 Python 파일 Ruff 통과. 기존 변경 파일은 기준 lint 22건에서 21건으로 줄었고 추가 진단은 없다.
Python 3.10 문법, 웹 script 구문, git diff whitespace 검사와 모델/선택 AST 불변 검사를 수행했다.

재현 환경: pytest, pytest-asyncio, numpy, onnx, aiohttp, pyusb, libusb1, msgpack, psutil;
`CARROT_JETSON`은 위 고정 SHA checkout으로 설정한다.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONUTF8=1 CARROT_JETSON=/path/to/carrot-jetson \
python -m pytest -p pytest_asyncio.plugin -o asyncio_mode=auto -o addopts='' --noconftest \
  openpilot/selfdrive/modeld/tests/test_jetlink_startup.py \
  openpilot/selfdrive/modeld/tests/test_camera_gap.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_ncm_transport.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_ncm_server.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_network.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_display.py \
  openpilot/tools/jetson/tests \
  openpilot/selfdrive/carrot/server/tests/test_jetson_local_status.py \
  openpilot/selfdrive/carrot/server/tests/test_jetson_diagnostic_receiver.py \
  openpilot/common/tests/test_jetson_status.py openpilot/selfdrive/ui/tests/test_jetson_badge.py \
  -q --tb=short -rs -k 'not test_service_start_failure_rolls_back_after_candidate_selection'
```

Linux에서는 위 Windows 전용 제외를 해제해 확인한다.
실제 장치 적용·오늘 저녁 확인 항목은 함께 제공한 `Jetson_실기확인_20261020.md`를 따른다.

Docs-Not-Needed: docs/user 가이드와 설정 Wiki는 변경하지 않고 요청한 개발 검토/실기 점검 문서만 제공한다.
