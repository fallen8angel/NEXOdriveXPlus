# NEXO Jetson 선별 포팅 조사

이 결과는 PC 코드·mock 검증 결과입니다. Comma4 + Jetson 실물 또는 실차 검증 완료를 뜻하지 않습니다.
NEXO 브랜치 커밋·push, 원격 feature push, merge, cherry-pick, 차량 설치는 수행하지 않았습니다.

기준: NEXO a6b5a756043d02f841a6dffff4e996aad2085d21.
비교: carrot-wip f104b56c41a7f5353f75b0cf6ebd45824c4ea93e.
Git 네트워크 fetch 실패로 GitHub 연결 도구에서 최신 ref·tree·변경 blob·commit을 읽고 Git object SHA를 검증하여 기준을 확보했습니다.

| 기능 | 현재 XPlus | 선별 반영 판단 |
| --- | --- | --- |
| NEXO native driving_supercombo·SHA·contract | 구현됨 | 유지 |
| local model 선행 실행·안전한 join·활성 장애 시 modeld restart | 구현됨 | 유지, hot-switch 변경 없음 |
| HUD·navigation 단일 USB owner | 구현됨 | 유지 |
| display-only 충돌 방지·선택적 stale gadget 해제 | 구현됨 | 유지 |
| Jetlink v2 | 구현됨 | 유지 |
| Jetlink v3 | 없음 | session별 엄격한 헤더·재연결 HELLO·native recurrent layout 적용 |
| 자동 재연결 | 고정 2초 retry | bounded backoff·HELLO timeout·cleanup 확인 보강 |
| NV12 직접 two-plane 변환 | packed copy 사용 | C4 1344×760 기존 freshness gate를 유지하며 변환만 개선 |
| LeadBrakeLights | 없음 | 기본 OFF, 독립 관찰·표시·로그만 추가 |
| 7000 진단 | 기존 연결·온도·HUD TX 진단 있음 | protocol·SHA·timing·retry·fallback 정보 추가 |
| DM·S3 HUD·Cinque/Mountain Dew·차량 제어 | 이번 범위 밖 | 제외 |

브레이크등 detector 원본은 d34224b49616882279d6b5f7884ce55f5a71ab82이며 별도 observer에서 사용합니다.
당근의 next_delay 변경, GPU/CPU affinity 변경, C3/C3X NV12 warp, eGPU fallback 정책은 가져오지 않습니다.
v3의 hidden state는 서버가 유지하며 WANT_HIDDEN으로 실제 전체 출력 벡터를 요구합니다. Native model의 scalars 크기는 contract에서 계산합니다.
모델 교체·모델 upload를 추가하지 않습니다. 준비되지 않은 서버는 local model 경로를 유지합니다.

## [1] 조사한 당근 커밋

| 커밋 | 내용 | 판단 |
| --- | --- | --- |
| [eb5bbf27](https://github.com/ajouatom/openpilot/commit/eb5bbf2720286f17fc55d6eab8a2f7aacecf1f3d) | v2 유지 및 v3 호환 | 엄격한 연결별 wire 버전과 WANT_HIDDEN 방식 선별 적용 |
| [3f2fe40a](https://github.com/ajouatom/openpilot/commit/3f2fe40ac104fac6ceacd2918fd40f42914ce3a0) | 자동 연결·앱 모델 선택 | 재탐색·연결 유지 원칙 참고, 앱 모델 선택·휴대전화 NCM 스택 제외 |
| [2f36166a](https://github.com/ajouatom/openpilot/commit/2f36166aee9df089ab2567e87434bd7849c6ef60) | hotplug 대기·표시 메모리 감소 | bounded 연결·cleanup 확인 참고, 기존 XPlus HUD owner 유지 |
| [d34224b4](https://github.com/ajouatom/openpilot/commit/d34224b49616882279d6b5f7884ce55f5a71ab82) | signal overhead 감소·브레이크등 관찰 | two-plane 변환과 detector만 선별 적용 |
| [62e15725](https://github.com/ajouatom/openpilot/commit/62e15725da51a0e5d3d4aebbe41a38952f03c703) | C3/C3X 직접 NV12 warp | 제외 |
| [0a25c684](https://github.com/ajouatom/openpilot/commit/0a25c684bb0f91e03da4b378821eed288fda6680) | eGPU background recovery | NEXO fallback 정책 보존을 위해 제외 |
| [d61728e8](https://github.com/ajouatom/openpilot/commit/d61728e8fdc9d3a83f30382fff994683164a8705) | S3 Wi-Fi H.264 HUD | 제외 |
| [f104b56c](https://github.com/ajouatom/openpilot/commit/f104b56c41a7f5353f75b0cf6ebd45824c4ea93e) | 조사 시점 carrot-wip 최신, S3 네트워크 보완 | 기준 확인용, 제외 |

## [2] 기존 구현으로 유지한 기능

NEXO driving_supercombo의 native spec·SHA, local modeld 선행 실행, 기존 안전한 join 조건,
활성 추론 장애 시 안전한 modeld restart, 기존 HUD·navigation의 단일 USB owner를 유지했습니다.
display-only 충돌 방지와 선택적 stale gadget 정리 스크립트도 그대로 유지했습니다.

## [3] 새로 반영한 기능

v2/v3 연결별 협상, 제한 시간 있는 HELLO, 증가형 reconnect backoff, cleanup 불확실 시 rebind 차단,
선택적 TCP endpoint, C4 two-plane 변환, 기본 OFF 브레이크등 관찰, 7000 native 진단을 추가했습니다.

## [4] 수정 파일

| 경로 | 변경 |
| --- | --- |
| openpilot/selfdrive/modeld/jetlink/compat.py | protocol 선택·retry 상태 |
| openpilot/selfdrive/modeld/jetlink/protocol_v3.py | v3 wire 정의 |
| openpilot/selfdrive/modeld/jetlink/client.py | native recurrent v3 요청·전체 출력 검증 |
| openpilot/selfdrive/modeld/jetlink/transport.py | 연결별 USB framing·cleanup 검증·TCP |
| openpilot/selfdrive/modeld/jetlink/daemon.py | 협상·복구·진단·단일 owner lock |
| openpilot/selfdrive/modeld/signal_tracking_shadow.py | packed NV12 중간 복사 제거 |
| openpilot/selfdrive/carrot/lead_brake_lights.py | 독립적인 C4 관찰 worker·표시 reader |
| tools/signal_analysis/lead_brake_detector.py | 중앙 제동등 후보·앞차 연관성 |
| openpilot/common/params_keys.h | LeadBrakeLights 기본 0 |
| openpilot/selfdrive/carrot_settings.json | 시험 설정 |
| openpilot/system/manager/process_config.py | ON·주행 중·C4일 때만 관찰 프로세스 실행 |
| openpilot/selfdrive/carrot/cluster/cluster_renderer.py | BRAKE? 후보 표시 |
| openpilot/tools/jetson/snapshot.py | 선택적 관찰 데이터 |
| openpilot/tools/jetson/hud.py | 호스트 시계 변환·표시용 reader |
| openpilot/selfdrive/carrot/server/services/jetson.py | native 진단 JSON |
| openpilot/selfdrive/carrot/server/features/jetson_pages.py | 진단 카드 |
| openpilot/selfdrive/modeld/tests/test_jetlink_compat.py | v2/v3 framing·wire mock·cleanup |
| openpilot/selfdrive/modeld/tests/test_nexo_jetlink_recovery.py | 실제 daemon·fallback 함수의 OS mock 검사 |
| openpilot/selfdrive/modeld/tests/test_signal_tracking_shadow.py | 정확한 픽셀·RGB 소유권 검사 |
| openpilot/selfdrive/carrot/tests/test_lead_brake_lights.py | 관찰·OFF·stale·회전·잘못된 프레임 |
| openpilot/selfdrive/carrot/server/tests/test_jetlink_inference_status.py | 진단 JSON·만료·SHA |
| openpilot/tools/jetson/tests/test_native_hud.py | 구형 Params allowlist·호스트 관찰 만료 |
| docs/nexo_jetson_selective_port_20261016.md | 비교·검증 보고 |

## [5] v2/v3 호환 방식

v2 protocol.py의 VERSION=2와 기존 기본 framing을 그대로 유지했습니다. daemon은 새 연결에서 v3를 먼저 시도하고,
HELLO가 실패하면 해당 stream을 닫은 뒤 다음 연결에서 v2를 시도합니다. 성공한 wire 버전은 유지합니다.
서버가 엄격한 단일 버전만 지원하므로 첫 탐색에서 한 번의 mismatch/timeout과 retry 지연이 생길 수 있습니다.
기존 v2 장비를 영구 거부하거나 같은 stream에서 헤더 버전을 바꾸지는 않습니다.

v3의 packet multiple은 512, v2는 1024이며 gadget의 16KB 정렬은 유지합니다.
v3 요청에서 서버 소유 prev_feat를 제외한 scalar 크기를 NEXO spec으로 계산합니다.
WANT_HIDDEN으로 실제 전체 출력 벡터를 받고 잘린 출력·NaN·다른 frame을 거부합니다.
모든 SHA·input/output shape·slice·checkpoint·frame_skip 비교를 유지하며 임의의 모델 upload를 추가하지 않습니다.

## [6] 자동 재연결

HELLO send/receive 제한은 각각 최대 3초이며 USB endpoint 준비 대기도 send deadline을 따릅니다.
실패 시 2→4→8→16→30초로 증가하고 이후 30초 간격으로 계속 재시도합니다. 각 sleep은 최대 1초로 나눕니다.
30초 이상 정상 서비스를 유지한 뒤 실패했을 때만 backoff를 초기화합니다.
cleanup 후 reader·watchdog 또는 UDC 소유권이 불확실하면 blocked 상태로 유지하고 재바인딩하지 않습니다.

기존 NexoJetsonUsb 확인과 stale nexo-display/jetlink 정리 스크립트는 변경하지 않았습니다.
AGNOS·adb·기타 network gadget을 강제 해제하는 동작을 추가하지 않았습니다.
선택적인 NEXO_JETLINK_TCP_HOST IP가 지정되면 TCP 5599만 재연결하며 해당 경로에서는 USB setup을 실행하지 않습니다.
정상 TCP 세션 중 USB 재탐색은 발생하지 않습니다.

당근의 휴대전화 전용 NCM 복합 gadget·DHCP·방화벽 초기화는 가져오지 않았습니다.
XPlus native Jetson bulk USB와 기존 offroad usb0 준비를 유지합니다.
NCM 이름을 포함한 초기화 실패 mock은 재시도 정책 검사이며 NCM 실물 스택 검증이 아닙니다.

## [7] LeadBrakeLights

기본 OFF이고 manager는 C4·onroad·ON일 때만 별도 observer를 실행합니다.
OFF이면 추가 카메라·subscriber·detector를 만들지 않으며 결과 파일을 polling하지 않습니다.
HUD의 가벼운 설정 확인까지 CPU 사용량이 수학적으로 0이라고 주장하지는 않습니다.

ON이면 일반 road NV12만 사용하며 중복·역행·150ms 초과 입력·다른 해상도를 거부합니다.
변환 전후 VisionIPC frame.frame_id와 client timestamp를 확인하여 재사용된 버퍼를 제외합니다.
신선한 radar/model/car/calibration, radar-vision 거리·횡방향 일치, 보정 상태와 3~35m 범위를 요구합니다.
방향지시등·회전·정차 중 큰 조향각·NaN·잘못된 앞차는 unknown입니다.
제한된 중앙 crop에서 동일 lead의 3개 증가 표본·최소 150ms 확인 후에만 candidate로 표시합니다.

최대 10Hz이며 관찰 프로세스는 한 CPU의 20%를 목표로 쉼 시간을 둡니다.
detector 작업이 5ms를 넘으면 unknown으로 버리고 500ms backoff합니다. 강제 중단이 가능한 실시간 deadline은 아닙니다.
결과는 candidate/unknown과 control_permission=False뿐이며 로그는 최대 1Hz입니다.
미검출을 OFF로 판정하지 않고 MED/SCC/출발/정지/CAN/Panda의 입력으로 연결하지 않았습니다.

HUD는 유효한 candidate만 BRAKE?로 표시하며 250ms가 지난 결과를 유지하지 않습니다.
Jetson으로는 선택적 최상위 확장 필드를 보내므로 구형 HUD Params allowlist를 깨지 않습니다.
구형 Jetson renderer는 이 필드를 무시하고 기존 HUD를 유지하며, 새 후보 표시는 renderer 업데이트가 필요합니다.
신호 검출 알고리즘이나 signal worker enable 조건에 의존하지 않는 별도 시험 observer입니다.

## [8] 7000 진단

기존 페이지에 native 진단 카드를 더했습니다. USB/TCP, Jetlink v2/v3, native 모델 이름,
요청·서버 보고·검증 SHA, SHA/contract 일치, ready/active, 왕복 지연과 GPU/queue/total ms,
HUD 연결과 TX age, navigation TX age, 현재 daemon의 reconnect 횟수, 마지막 연결 실패,
local model 사용과 사유를 보여 줍니다. 기존 온도·thermal·단계별 진단은 유지합니다.
상태는 정상/대기/경고/오류이며 확인되지 않거나 만료된 값은 null/확인 불가입니다.
서버가 장치 모델명을 보내지 않으면 이름을 추정하지 않습니다. TX는 수신·표시 완료 확인과 구분합니다.

## [9] PC 테스트 결과

최종 회귀 실행: **393 passed, 1 failed, 2 skipped, 19 deselected, 10 subtests passed**.
실패는 기존 test_spas_rear_positions_are_mapped_to_display_stages_and_expire입니다.
원본 NEXO의 PARKING_TIMEOUT_S=1.5와 테스트의 1.01초 만료 기대가 다릅니다.
수정 전 소스를 Git에서 직접 읽어 같은 assertion 실패를 재현했으며 해당 코드·기대값을 수정하지 않았습니다.
건너뛴 2건은 빌드된 Linux cereal/msgq/HUD 런타임이 필요한 검사입니다.
제외한 19건은 HTTP/비동기 OS 네트워크 검사가 중심이며 이 환경의 로컬 socket 생성이 멈추어 실행하지 않았습니다.
초기 긴 임시 경로로 발생한 Windows 파일 경로 오류는 짧은 허용 Temp 경로로 재실행하여 해소했습니다.

| 요구 검사 | 결과와 범위 |
| --- | --- |
| Python syntax | 변경·추가 Python 20개 통과 |
| Ruff | 새 진단 0개, 원본과 동일한 기존 진단 21개는 보존 |
| 기존 Jetlink tests | 기존 HUD/inference 순서·pinned v2 서버 Session mock 포함 통과 |
| v2 protocol | raw header·padding·분할/연속 read 통과 |
| v3 compatibility | scalar 크기·WANT_HIDDEN·전체 출력·이상 응답 거부 통과 |
| v2 / v3 서버 mock | 실제 client와 framing, socket primitive만 메모리 대체하여 통과 |
| 자동 협상 | v3 선호 및 구형 v2로 새 stream 재시도 통과 |
| disconnect/reconnect·late boot | 실제 daemon 함수에서 OS/시간/장치를 대체하여 통과 |
| model SHA mismatch | ready 거부·진단 mismatch 검사 통과 |
| HUD + inference | 동일 owner와 추론 응답 우선 순서 통과 |
| navigation | 동일 owner의 제한된 media 전송 통과 |
| 7000 JSON | 직렬화·만료·SHA·ready/active 구분 통과 |
| 7000 화면 | JavaScript syntax 및 mock DOM의 v3/SHA/active 렌더링 통과; 실브라우저 HTTP 미검증 |
| Lead OFF | camera/context/detector/file polling 없음 검사 통과 |
| Lead ON | stale·잘못된 geometry·재사용 frame·잘못된 lead·turn·unknown/candidate 검사 통과 |
| 기존 fallback | 초기 장애 시 local 유지, 활성 장애 시 안전한 modeld restart 정책 통과 |
| 보호 대상 diff | 아래 [12]의 경로 변경 없음 |

PC NV12 측정은 padded 무작위 C4 크기 영상과 OpenCV 단일 thread를 사용했습니다.
300회 변환의 지연 중앙값은 packed **1.22785ms**, two-plane **0.82820ms**로 약 32.5% 감소했습니다.
7개 80회 batch의 thread CPU 평균/프레임 중앙값은 **1.36719ms → 0.97656ms**였습니다.
Windows 개별 CPU timer는 양자화가 커서 단일 frame CPU 중앙값은 해석하지 않았습니다.
픽셀 완전 일치와 원본 버퍼 변경 후 RGB 소유권 유지를 검증했습니다.
측정치는 PC 변환 microbenchmark이며 차량 전체 CPU, 신호 인식률·FPS·end-to-end 지연 개선을 증명하지 않습니다.
별도 NumPy 색 변환을 쓰는 구형 signal_color_shadow 경로는 픽셀 결과가 바뀔 수 있어 변경하지 않았습니다.

## [10] 전체 빌드

실행 가능한 Linux/AGNOS 빌드 환경과 SCons가 없습니다. Python -m SCons 호출은 No module named SCons로 종료했습니다.
WSL은 설치되어 있지 않고 Git Bash 구문 검사도 Windows sandbox의 MSYS 객체 생성 권한 오류로 시작하지 못했습니다.
전체 빌드는 미실행이며 이번 C++/native 빌드 오류인지 판정할 결과가 없습니다.
LeadBrakeLights Params 키 사용 전 정상 Linux 빌드가 필요합니다.

## [11] 실물·실차에서 필요한 검사

Comma4 먼저 부팅/Jetson 늦은 부팅, 긴 TensorRT 준비, USB 분리/재연결, Jetson reboot·전원 OFF/ON,
USB enumeration 실패, TCP 실제 endpoint 복구와 기존 USB owner 보존을 확인해야 합니다.
v2 및 실제 v3 서버의 native ONNX SHA/contract·recurrent 수치 일치·지속 추론 latency·온도·부하,
HUD/navigation 동시 출력과 새 renderer의 후보 표시를 검증해야 합니다.
주차 상태에서 활성 추론 장애 시 기존 안전한 restart/fallback을 확인한 후 별도 실차 검증이 필요합니다.
브레이크등 후보의 차량별 형상·야간·비·LED aliasing·오검출 성능은 검증하지 않았습니다.

## [12] 보호 대상 확인

steering·longitudinal·MED·SCC·radar 구현·Panda·CAN·safety·NEXO vehicle interface 변경이 없습니다.
DM 모델·DM UI·DM Params·얼굴/시선·DM GPU 정책 변경이 없습니다.
주행 모델 파일과 modeld.py, Jetlink model.py/spec.py, setup_gadget.sh도 그대로입니다.
process_config.py에는 새 관찰 프로세스와 predicate만 추가했고 Params에는 LeadBrakeLights만 추가했습니다.
Cinque/Mountain Dew 교체, S3 HUD/firmware/Wi-Fi H.264를 반영하지 않았습니다.
git diff --check 통과이며 기존 주차·side/reverse 카메라 설정·CAN 해석은 유지했습니다.

## [13] 브랜치와 커밋

브랜치: **feature/nexo-jetlink-v2-v3-observer**.
부모 커밋은 원격 NEXO a6b5a756043d02f841a6dffff4e996aad2085d21입니다.
이 보고서를 포함한 최종 로컬 commit SHA는 최종 응답과 Git 기록에서 확인할 수 있습니다.
원격 push·NEXO 병합과 차량 적용은 하지 않았습니다.
