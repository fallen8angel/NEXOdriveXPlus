# NEXO 외부 HUD 통신 안정화 비교·검증 (2026-10-28)

## 판정과 범위

코드에서 재현 가능한 결함을 수정했다. **10월 28일 관측된 HUD 끊김 5회의 실제 원인이 모두 해결됐다고 확정할 수 없다. 실장비 검증 필요.** 당시 원본 로그와 차량 작업 트리는 이 작업 환경에 제공되지 않았다. 사용자가 제공한 평균 10.58초/최대 17.39초를 이번 로컬 시험 수치와 직접 비교하지 않는다.

- NEXO 기준: `2bde6a0967c67146bea85bb7b439efae85ccafef` (원격 NEXO와 차량 보고 커밋 일치).
- 당근 비교: `604bc0a22733e648ec05b268da1ffaae7db52a69` (`carrot-wip`, 작업 시작 시 가져온 HEAD).
- 기존 젯슨 vendor 유지: `ajouatom/carrot-jetson` `d0dc96fa32410403dfb2fefff3e2909c11ec109b`.
- feature: `codex/nexo-hud-link-stability-20261028`. NEXO 브랜치에 병합·푸시하지 않는다.
- 새 작업 폴더의 clone은 clean. 차량의 dirty=True는 차량 원본 `git_status.txt`, `git_diff.patch` 없이는 판정 불가. 기존 사용자 작업본을 수정하거나 삭제하지 않았다.

## 실제 비교 결과

| 계층 / 함수 | 당근 최신 구현 | NEXO 기존 및 이번 처리 |
|---|---|---|
| USB 경로 | 일반 Jetlink USB/FunctionFS, 별도 TCP·mobile 전송 선택 | NEXO의 명시적 NCM HUD-only 경로와 다른 구조. 전체 transport/daemon 교체 안 함 |
| NCM 준비 | 당근 USB 문서는 일반 USB display 복원 설명이며 NEXO NCM 패치 문서가 아님 | `network.ensure_ncm_network`, `protected_ncm.network_once`: NIC/ifindex/driver/VID·PID 확인→link up→기존 주소→peer /32 route→carrier/route 확인 이미 있음. 그대로 유지 |
| TCP 연결 | `third_party/jetlink/jetlink/transport/tcp.py`는 일반 TCP transport | `ncm_display.connect_display_ncm`: 기존 source IP, direct USB route, 연결 전후 인터페이스 검증 유지. 192.168.100.127/.250 및 5599 유지 |
| 협상/수신 확인 | `tools/jetlink/server.CarrotSession`, HUD/NAVI/USB capability | NEXO `DisplaySession`, `HudReceipt`: 명시적 display-only/nexo HUD capability, 증가 sequence, exact HUD ack 이미 있음. wire version/capability 변경 없음; 새 진단 필드는 optional |
| HUD 생성·송신 | `hud_publisher.main`, `hud_protocol.Publisher.packet`: 10Hz, 96KiB, 동일 timestamp 중복 제거, 0.3초 freshness | NEXO `DisplayPublisher.packet/send`: 이미 동일 제한. 기존 32KiB media fragment, 최대 2개, 12ms admission budget 유지. 이 budget은 OS write 완료 hard deadline 보장이 아님 |
| GPU window 전송 | `modeld/jetlink/daemon.publish_hud` 등에서 HUD와 media를 inference window에 배치 | NEXO 제어/추론 scheduling에 영향을 줄 수 있어 복사 안 함. 기존 모델 reply 이후 전송 유지 |
| watchdog | `WriteWatchdog._waiting_until`: 기존 더 이른 wake 재사용, disarm에서 notify 안 함 | 이전 NEXO는 모든 arm/disarm에서 notify. 이 제한된 수정과 thread test 6개만 이식. 만료·priority·callback 조건 유지. **NCM TCP 자체가 이 FunctionFS watchdog을 사용한다는 뜻은 아님** |
| 카메라/worker 종료 | 최신 `hud_protocol.Publisher.close`에도 kill 이후 bounded wait가 있으며 이 예외가 모두 차단된 것은 아님 | NEXO의 camera→vehicle Publisher→display worker 경로는 두 번째 TimeoutExpired가 밖으로 전파됨. 이번에 독립 보완 |
| 서비스 시작 | 당근 protected storage/update/service order | NEXO protected_ncm의 NetworkManager/storage→NCM worker→server 순서 이미 있음. Type=simple의 After만으로 NIC ready를 보장하지 않으므로 기존 TCP opener/ready 재확인·재시도 유지 |
| 업데이트 | 최신 당근 signed release/model 선택 | 기존 NEXO 보호형 bundle/검증/rollback 및 vendor/model pin 유지. 당근 최신 runtime 자동 적용 안 함 |

직접 확인한 문서:
- [USB HUD 복원 문서](https://github.com/ajouatom/openpilot/blob/604bc0a22733e648ec05b268da1ffaae7db52a69/docs/jetson_usb_hud_restore_20261021.md): carrot-nav 중심 변경이며 wip에는 기록용이라고 명시. 문서의 4ms 추가 fragment 조건/실장비 결과를 NEXO 구현·성능으로 간주하지 않음.
- [watchdog 지연 문서](https://github.com/ajouatom/openpilot/blob/604bc0a22733e648ec05b268da1ffaae7db52a69/docs/jetlink_watchdog_latency_20261021.md): `_waiting_until` 및 notify 동작을 실제 최신 소스와 대조함.

## 수정 내용과 고장 구간 분리

1. `tools/jetson/process.py`, `camera.py`, `vehicle.py`, `modeld/jetlink/display.py`: TERM→bounded wait→KILL→bounded wait. 두 번째 timeout도 기록 후 단일 background reaper가 회수. 이미 종료/중복 요청 처리. 회수 중인 자식을 대체하여 계속 늘리지 않도록 optional NCM/legacy owner가 대기. 카메라 종료 오류가 HUD 스냅샷 worker로 전파되지 않음. timeout을 늘려 숨기는 수정이 아님.
2. `display.py`, `ncm_display.py`: 죽은 snapshot worker를 같은 TCP session 안에서만 최대 3회 복구. 30초 신선한 출력 후 budget 회복. 원래 media worker/프로토콜 유지. TCP 실패 시 2→4→8→16→30초 대기; transport reconnect와 service restart를 구별.
3. `hud.py`, `supervisor.py`: renderer 앞에서 최대 2초 데이터 대기. stale snapshot을 새 데이터처럼 송신하지 않음. 장기 입력 손실은 exit 75로 구별하여 panel 실패 budget에 넣지 않음. 준비된 패널이 없으면 기존 VID/PID 검사로 대기하여 프로세스를 반복 생성하지 않음.
4. 신선한 표시 요청이 계속 있는데 panel write가 10초 이상 없으면 해당 renderer만 TERM/KILL/reap. 정상 첫 기동을 복구 성공으로 세지 않으며 재시도 후 2초 fresh panel evidence가 있어야 성공 집계. 연속 실패 3회에서 blocked. 서비스 재시작으로 실패 budget이 초기화되지 않도록 RAM journal 유지. 30초 건강한 출력 후 연속 실패 초기화. 전체 Jetlink/GPU/차량 서비스 재시작 없음. Linux renderer 전용 process group과 parent-death signal로 encoder 자식 정리; 실제 Linux lifecycle 확인은 별도 필요.
5. `health.py`, `state.py`, `ncm_display.py`, 7000 `jetson_diagnostics.py`, `nexo_jetlink_diag.py`, `nexo_hud_link_log.py`: local 송신/peer reply 시각, remote HUD 수신 age, panel write age, 협상 상태, source revision, host/renderer PID, shutdown 오류, 연결 이상/복구 집계. 서로 다른 장치 monotonic clock을 빼지 않음. 응답 경과시간을 remote age에 추가. 오래된 증거는 현재 connected로 표시하지 않음. 구형 젯슨에서 제공되지 않는 필드는 unknown/None.
6. `nexo_long_logger.py`, `diagnostic_capture.py`: 실제 msgq `ipc_pyx.pyx:Poller.poll`이 새 Python wrapper를 반환하는데 기존 수집기는 `id(sock)`로 서비스 조회하여 skip할 수 있었음. Poll은 wakeup으로만 사용하고 원래 socket에서 직접 nonblocking read. 서비스당 256건 제한으로 CAN이 다른 상태 수집을 독점하지 않음. 구독 실패를 서비스별 기록하고 수집 counts/no_messages 상태 제공. CAN parser/송신/제어 코드는 수정 안 함.

패널 write 완료는 **소프트웨어 전송 완료**이다. 실제 패널의 빛/픽셀 표시 ACK가 없으므로 `physical_hud_verified=false` 유지. USB carrier 정상만으로 TCP/HUD 정상이라고 판정하지 않는다. PID는 실행 관측이며 systemd unit 자체의 ActiveState 증거는 기존 원격 서비스 진단을 이용한다.

## 기존 기능 보존

Snapshot 서비스/Params allowlist, NEXO renderer, 객체·전방차·보행자/오토바이·방향지시등·신호등·후측방·후진 카메라·속도·내비 데이터 형식/표시 로직은 변경하지 않았다. 기존 budget 초과 시의 선택적 snapshot 항목 제거 정책도 그대로다. 회귀 테스트는 데이터 계약·기존 신호 검출/표시 전달·SPAS12·side/reverse 설정 보존을 확인하며, 실제 차량의 모든 시각적 표시 정상 작동을 보증하지 않는다. 없는 기능을 추가하지 않았다.

DM, opendbc/넥쏘 CAN·Panda 안전, 롱컨, MED, 크루즈 버튼, 조향 튜닝, 레이더/SCC, vNAVI, 모델 파일의 변경 없음. 안전 조건 완화나 우회 없음.

## 검증 결과

- 변경 Python 21개 문법 compile 검사 통과. `git diff --check` 통과. 보호 경로 변경 없음 확인.
- 최종 집중/회귀: **317 passed / 2 skipped / 0 failed**. Windows Python 3.12.14. skip 2개는 Linux 빌드 환경에서만 실행되는 실제 cereal/msgq/HUD/manager import 검사. 전체 openpilot native import 통과라고 표시하지 않는다.
- 테스트 범위: USB delayed discovery/주소/route 준비 대역, TCP 실패 및 같은 port 복구, 실제 loopback 500회 reconnect, HUD ack/freshness/sequence, panel evidence와 수신 분리, renderer stalled/exit/복구 상한/late panel, 카메라 TERM/KILL timeout 대역 및 실제 자식 회수, watchdog 실제 thread 1,000 writes, 표시 회귀, 수집 fairness/소켓 identity 회귀.
- 기존 400회 실패 HUD session의 Python reference 회수 검사 포함.
- 120초 실제 경과시간 loopback soak 수행. 정상 USB/차량/Panda를 연결한 정차 시험이 아니라 synthetic snapshot+모의 panel-write telemetry 시험. 시간·송신 수·복구 시간·RSS 및 자식/thread count는 함께 제공한 `soak-result.json` 참고.
- 초기 실패 중 PYTHONPATH/tempdir/샌드박스 loopback/미설치 cv2·psutil은 시험 환경 문제를 수정하고 재실행. 현재 결과에 실패를 숨겨 pass로 세지 않음. pytest 환경 plugin 설정 warning 3개는 남아 있음.
- 실장비 검증 필요: USB 물리 재연결, 실제 NCM route/MTU/전원, Jetson systemd/service 시작·재부팅, Linux TERM/KILL/process group, graphics/ffmpeg/USB 패널 실제 응답, camera VisionIPC 종료, 기존 표시의 모든 모드, 120초 이상 정차 및 이후 장시간 실차 안정성/메모리. 실제 롱컨 제어는 이번 자동화 시험의 검증 범위 밖.

## 설치와 되돌리기

**콤마만 업데이트하면 카메라 종료/송신 worker/7000 수집기/watchdog 수정은 적용되지만, 젯슨 renderer supervisor와 새 원격 진단에는 젯슨도 별도 업데이트가 필요하다.** 기존 보호형 NEXO 설치는 일반 당근 자동 업데이트로 교체되지 않는다.

1. 차량 정차·실제 offroad에서 작업. `/data/openpilot`의 `git status --porcelain=v1 --untracked-files=all`과 `git diff`, `git diff --cached`를 먼저 보관. dirty이면 사용자 변경을 검토·보존한 뒤 전환하며 `reset --hard`, `clean` 사용하지 않음.
2. `git fetch origin codex/nexo-hud-link-stability-20261028` 후 깨끗한 작업본에서 `git switch --track origin/codex/nexo-hud-link-stability-20261028`. 이미 같은 local branch가 있으면 현재 변경을 보존하여 해당 branch로 전환. NEXO에 merge/push하지 않음. 콤마 재시작 후 commit 확인.
3. 제공 bundle 및 같은 commit의 installer/helper/manifest/SHA256SUMS를 젯슨 DATA의 기존 `updates/nexo-incoming`에 복사. 현재 `/opt/carrot-jetlink/updates/nexo-ncm.json`, current/SOURCE_COMMIT 및 실제 콤마 ONNX SHA를 확인. 기존 display_only 여부, interface, model SHA를 그대로 사용. 새 IP/VID/PID/모델을 지정하지 않음.
4. 기존 보호형 명령: `sudo python3 protected_install.py install --bundle <bundle.tar.gz> --sha256 <manifest의 bundle_sha256> --ncm-interface <기존 interface> --model-sha256 <현재 콤마와 일치하는 기존 SHA> --confirmed-offroad`. 기존 구성 `display_only=true`인 경우에만 `--display-only` 추가. 기존 방식이 보호형 설치가 아니면 이 명령을 실행하지 않고 설치 방식부터 확인.
5. `NEXO_INSTALL_OK`, current/SOURCE_COMMIT, DATA install-status, 실제 service ExecStart, 7000 `hud_runtime`의 양쪽 revision/tx/rx/panel age 확인. 패널 실제 화면은 사람이 확인. 엔진/모델 enable, UDC reset, signed 당근 runtime 교체 작업 없음.
6. 설치 실패는 기존 installer rollback 이용. 수동 복귀는 현재 installer의 `rollback --confirmed-offroad`, 콤마는 보존한 기존 branch/commit으로 복귀. 3회 renderer 실패로 blocked면 원인을 해결한 뒤 해당 HUD service를 중지하고 `/dev/shm/nexo-jetson/hud-recovery.json`만 별도로 보관·제거 후 다시 시작하거나 재부팅. 자동으로 반복 서비스 재시작하지 않음.

**완료 판정:** 예외 전파·watchdog 불필요 wake·collector identity 결함은 코드 및 자동화로 수정 확인. 관측된 5회 끊김의 전체 인과관계와 실제 HUD 끊김 해소는 추가 실장비 검증이 필요하다.
