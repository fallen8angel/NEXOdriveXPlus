# NEXO USB-NCM Jetlink 통신·상태 수정 — 2026-10-22

브랜치: `codex/jetlink-ncm-status-20261022`. 기준 NEXO: `a44899cddf8229656926a9c5a8f54a50edacdb92`.
비교 carrot-wip: `4920f270b56ad472c2f90ddf24f2643b19e7af37`.
고정 Jetson 서버: `d0dc96fa32410403dfb2fefff3e2909c11ec109b`.

NEXO에는 이전 `0c29d2ac`, `f3e8fd42`뿐 아니라 PR #32의 USB/HUD 관찰과 USB 자동 연결 timer가 추가돼 있다. 최신 NEXO에서 새 feature를 만들었다. 기존 observer branch `feature/nexo-jetlink-v2-v3-observer`도 확인했으나 차량 표시·관찰 기능과 v3 변경을 포함하므로 일괄 병합하지 않았다. NEXO 메인과 장비에는 이번 변경을 적용하지 않는다.

## 확인된 사실과 미확인 사항

사용자 보고: USB-NCM `.127 ↔ .250` ping/재부팅 복구 성공, Jetson `--backend trt --transport usb`, TensorRT 10.3 엔진 준비 로그. 현재 장비가 분리돼 SSH 확인 불가. **실제 NEXO 모델 로드 여부는 미확인**이다. 예전 TensorRT 실패가 현재도 계속된다고 단정하지 않으며, 준비된 엔진이 NEXO라고도 단정하지 않는다.

Bulk 서버는 `1209:0001`을 기다리지만 현재 장치는 `04d8:1234` CDC-NCM이다. ping 성공은 IP 계층의 증거이고 Jetlink HELLO, 모델 계약, 영상 입력/추론 출력, TURZX 표시를 증명하지 않는다. 보호형 NCM 설치가 선택된 실제 서비스는 `--transport tcp --host 192.168.100.250 --port 5599` 및 NEXO wrapper 환경을 사용해야 한다. 실행 중인 Bulk 서비스를 코드만으로 TCP라고 표시하지 않는다.

코드에서 확인한 문제:

1. `_apply_local_jetlink_context()`가 추론 OFF이면 조기 반환하여 물리 USB 링크까지 DISCONNECTED로 남겼다.
2. PR #32의 상태 GET은 매번 5599에 TCP 접속 후 종료했다. 단일 세션 서버를 불필요하게 점유할 가능성이 있고, 열린 포트는 JLNK 프로토콜 검증도 아니다.
3. 정상 native inference의 이미지 요청 완료를 CAMERA TX 증거로 전달하지 않아 Camera와 CAMERA TX 표시가 달라질 수 있었다.
4. 모델 active 파일이 갱신되는 동안 실제 추론 응답이 끊겨도 STATE 응답만으로 active가 유지될 수 있었다.
5. `enable-ncm-tcp`는 기존 USB 경로를 확인하지만 실제 서버의 HELLO/STATE, resident NEXO SHA를 확인하기 전에 enable marker를 만들었다.

## 재사용한 실제 통신 경로

| 과정 | 기존 NEXO 소스 |
|---|---|
| 카메라 입력 | `model.JoiningModel.run()` → `warp.Warp` → `link.Client.infer()`의 image/packed context |
| comma 송수신 | `daemon._serve_modeld()` → `client.JetlinkClient.infer()` → `ncm_transport.JetlinkNcmTcpTransport` |
| Jetson TCP | `native_host.main()` → `ncm_server.tcp_opener()` → 고정 Carrot `Session`/`PolicyQueues`/TensorRT backend |
| 계약 검사 | `JetlinkClient.ensure_engine()`의 전체 ModelSpec 비교, daemon의 2회 reset warmup; 업로드 없음 |
| 응답 검사 | sequence/frame ID/status/출력 길이/finite 검사, 기존 frame deadline |
| 재연결 | daemon cleanup/2초 재시도, listener ifindex 재탐색, 양쪽 기존 NCM 복구 worker |
| HUD | 기존 DisplayPublisher/snapshot/navigation/HUD renderer/panel lock. 레이아웃 변경 없음 |

카메라 전송·추론 수신을 새로 중복 구현하지 않았다. 기본 OFF와 기존 모델 참여·실패 처리 정책도 그대로다. `.127` source 및 검증된 USB NIC에 bind하는 기존 TCP transport를 재사용하여 Wi-Fi 경로로 자동 우회하지 않는다.

[최신 carrot-wip daemon](https://github.com/ajouatom/openpilot/blob/4920f270b56ad472c2f90ddf24f2643b19e7af37/openpilot/selfdrive/modeld/jetlink/daemon.py)은 HELLO/모델 준비/세션/재시도를 구현하며, [client](https://github.com/ajouatom/openpilot/blob/4920f270b56ad472c2f90ddf24f2643b19e7af37/third_party/jetlink/jetlink/client.py)와 [TCP transport](https://github.com/ajouatom/openpilot/blob/4920f270b56ad472c2f90ddf24f2643b19e7af37/third_party/jetlink/jetlink/transport/tcp.py)를 가진다. 이전 비교 HEAD 이후 주된 Jetlink 변경은 USB scheduling/watchdog wakeup 최적화다. g1/NCM 전환이나 NEXO 계약을 자동 해결하는 변경이 아니므로 복사하지 않았다. NEXO의 검증된 v2 계약을 유지하며, 다른 runtime/v3는 사전 검사에서 실패하도록 한다.

## 이번 변경 파일

| 파일 | 변경 |
|---|---|
| `openpilot/selfdrive/modeld/jetlink/check_ncm.py` | 명시적 정차 진단: 실제 USB 전용 TCP HELLO/STATE, resident SHA와 로컬 ONNX hash·TRT backend·표시 전용 여부 확인. ENGINE/INFER/build/upload/service 변경 없음. |
| `openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh` | 기존 명시적 enable-ncm-tcp 앞에 사전 검사 추가. 실패 시 enable marker/전송 모드/USB 변경 전 중단. 검사 후 offroad 재확인. |
| `openpilot/selfdrive/modeld/jetlink/daemon.py` | 실제 modeld 이미지 요청/응답 완료 횟수와 전송 완료 시각을 상태에 추가. warmup은 제외. 추론 바이트·출력 순서 변경 없음. |
| `openpilot/selfdrive/carrot/server/services/jetson.py` | 검증된 native 이미지 교환을 CAMERA TX에 반영. 실제 캡처 timestamp를 지어내지 않음. 최근 inference 응답 없는 active 상태는 해제. |
| `openpilot/selfdrive/carrot/server/features/jetson.py` | 추론 OFF여도 물리 USB 별도 표시. OFF/미실행과 앱 응답 미확인을 구분. 상태 GET의 5599 접속 제거, fresh 기존 세션 기록만 관찰. |
| `openpilot/selfdrive/modeld/tests/test_jetlink_ncm_check.py` | 잘못된 SHA/backend/표시 전용 호스트, 소유권/정차 guard, 오류 cleanup 검사 |
| `openpilot/selfdrive/modeld/tests/test_jetlink_ncm_server.py` | 실제 pinned server+localhost TCP에 비균일 이미지 바이트 검증 및 HELLO/STATE 사전 검사 추가 |
| `openpilot/selfdrive/modeld/tests/test_jetlink_display.py` | 실제 요청 완료만 카메라 상태로 기록하며 추론 응답이 HUD보다 우선하는지 검사 |
| `openpilot/selfdrive/carrot/server/tests/test_jetson_local_status.py` | OFF 상태 USB 표시와 GET이 TCP 연결하지 않는지 검사 |
| `openpilot/tools/jetson/tests/test_management.py` | 이미지 TX와 오래된 active 상태 구분 검사 |
| 이 문서 | 비교, 적용 전제, 검증 결과 |

신규 능동 진단은 자동 실행하지 않는다. 기존 enable-ncm-tcp 명령을 명시적으로 실행할 때만 실행된다. 추론/표시 owner가 활성화됐거나 offroad를 확인할 수 없으면 접속하지 않는다. HELLO는 새 세션 초기화이므로 장비에서 owner가 동시에 시작되지 않는 정차 작업으로 수행해야 한다. 상태 페이지의 일상적 조회는 이 진단을 실행하지 않는다.

`eligible=true`는 protocol/응답/resident SHA/TRT backend의 사전 확인일 뿐, engine contract·실제 inference·physical HUD 성공이 아니다. 이후 기존 daemon이 전체 모델 계약과 warmup을 별도로 확인한다. 가짜 peer나 잘못된 네트워크에서의 인증을 제공하는 기능도 아니며, 기존 전용 USB route/ifindex containment를 유지한다.

## 검증

**552 passed, 4 skipped, 0 failed**. 기존 관련 회귀와 새 17개 테스트를 포함한다. 핵심 TCP/상태 검사 122개도 별도로 통과했다. Python Ruff, Bash `-n` 구문 검사 통과. Windows sandbox에서 Bash 실행 권한 오류가 있었지만 정상 권한에서 구문 검사만 다시 수행해 통과했다. 장비용 스크립트는 실행하지 않았다.

실제 localhost TCP + 고정 Carrot session/queues로 비균일 이미지 입력 바이트와 응답, 세션 재연결을 시험했다. **GPU는 대역**이다. 실제 NEXO ONNX CPU 회귀와 보호형 설치/rollback simulated tests, g1 충돌, 네트워크 복구/이름 변경/기본 경로 보호 검사를 유지했다.

4 skip: pyray 1개, 빌드된 Linux cereal/msgq 2개, 실제 Linux flock 1개. pytest/aiohttp 기존 경고 19개. 실제 systemd/Jetson GPU/TURZX/주행 시험 결과가 아니다.

MED/SCC, CAN/DBC, radar, 조향/제동/정차 제어, 운전자 감시 관련 파일, 모델, HUD layout, 보호형 installer 및 네트워크 자동 복구 구현은 변경하지 않았다. NEXO ONNX SHA는 `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`다.

## 장비를 다시 연결한 후 확인

1. Jetson `current/SOURCE_COMMIT`, `cache/last-loaded.json`, `systemctl show carrot-jetlink -p ExecStart` 확인. 현재 resident 모델이 NEXO인지 미확인 상태를 해소한다.
2. 기존 protected installer의 NCM 구성/후보 검증/rollback으로 TCP host를 선택한다. `/etc` 일반 installer 또는 서비스 argv만 임의 변경하지 않는다. 이 feature는 보호형 root에 쓰지 않고 자동 설치도 하지 않는다.
3. 양쪽 IP/USB peer route와 `.250:5599` listener를 확인한다. 추론·NCM 표시·legacy 표시 owner를 비활성화하고 정차 상태에서 아래 one-shot 진단을 실행한다. 이 명령은 enable 파일을 만들지 않는다.

```sh
cd /data/openpilot
PYTHONPATH="$PWD" /usr/local/venv/bin/python3 -m openpilot.selfdrive.modeld.jetlink.check_ncm --confirmed-offroad
```

실제 환경의 Python 경로가 다르면 기존 환경의 interpreter를 사용한다. 서버가 Bulk인 상태, 잘못된 모델, 미지원 protocol, 표시 전용 host는 실패로 남긴다. 임의 재설치/빌드를 반복하지 않는다.

4. 별도 실기 검증 승인 후에만 기존 `nexo_jetlink_ctl.sh enable-ncm-tcp` 사용을 검토한다. 이는 원격 추론을 활성화하는 명령이므로 이번 작업에서 실행하지 않았다. 정상 확인 전 기본 OFF 유지.
5. 실제 카메라 전송/응답 지연·중단 복구, comma/Jetson 단독 및 동시 재부팅, USB 탈착/이름 변경을 확인한다. 상태 표의 USB, JLNK HELLO, Camera TX, Inference, HUD TX와 패널 출력을 각각 관찰한다.
6. TURZX `1cbe:0092` 열거·기존 HUD service·fresh hud-status·실제 화면을 별도 확인한다. TCP 응답이나 HUD TX만으로 실제 화면 검증됨으로 표시하지 않는다.

모든 장비 항목은 **실차 미검증**이다. 코드상 메인 검토에 필요한 회귀는 통과했지만, 실차 추론 활성화 가능 판정은 보류한다. 사용자 지시에 따라 이번 feature는 NEXO 메인에 병합하지 않는다.
