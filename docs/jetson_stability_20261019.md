# NEXO Jetson · USB HUD 안정화 선별 통합

## 기준과 변경 경계

- NEXO 기준: `a6b5a756043d02f841a6dffff4e996aad2085d21`.
- 기존 Jetson 개발 브랜치: `ba8564070db53ba6b7ab5d236cbf80f830905fdf`.
  이 커밋은 NEXO의 조상이다. 기존 코드를 다시 병합하거나 이전 상태로 되돌리지 않았다.
- 작업 시작 시 확인한 당근 `carrot-wip` HEAD:
  `fa553468ef6f86a610a9053adbf3d23788f5aef5`.
  요청서의 `2cd1d7978d` 이후 커밋까지 확인했다.
- 작업 브랜치: `codex/jetson-stability-20261019`.
- 차량 제어, MED/SCC, CAN, DBC, 정지선, 기어, 주차센서, 방향지시등,
  vNAVI 제어, 모델 선택/추론 정책과 운전자 감시 코드·설정은 수정하지 않았다.
  HUD 화면 배치와 데이터 항목도 유지한다. 7000 웹 진단에만 관측 정보를 추가한다.

## 기존 연결 구조와 재사용

소스로 확인한 구조이며, 실제 차량 배선이나 전류를 측정한 결과가 아니다.

1. 현재 native Jetlink 경로는 comma의 FunctionFS JLNK gadget과 Jetson USB host를 연결한다.
   Jetson의 기존 고정 버전 Carrot server가 추론을 처리하고, NEXO snapshot adapter가
   HUD 데이터를 읽는다. 고정 서버 버전 `d0dc96fa32410403dfb2fefff3e2909c11ec109b`는 유지한다.
2. USB HUD는 Jetson USB-A host 쪽에 별도로 연결된다. 기존 렌더러와 ffmpeg/libx264,
   실제 지원 VID/PID 탐색 및 `hud.lock` 단일 소유권을 재사용한다.
3. 대체 display-only NEXD 모드는 기존 `NexoJetsonUsb` opt-in이다. USB heartbeat,
   세션 nonce, bounded retry journal, 영상 source epoch와 keyframe gate를 재사용한다.
   `NexoJetsonUsb=0` 기본값과 native 경로와의 상호 배제는 그대로다.
4. 직접 YOLO 경로는 roadEncodeData → HEVC → 기존 YOLO이다. USB 선택기 사용 시
   로컬 단일 입력을 구독하고, 그 외에는 명시적으로 설정한 comma 주소를 구독한다.
   기존 상태 비콘, `/api/jetson/status`, `/jetson`과 통합 TXT 진단을 확장한다.

comma·Jetson의 독립 전원 유무, USB-C 전원 역할, HUD 소비전류와 PMIC 호환성은
이 개발 환경에서 확인할 수 없다. 소프트웨어상의 연결 구조가 전기적 안전성 증명은 아니다.

## 당근 참고 기능별 판단

| 참고 커밋 | 판단 | 반영 범위 또는 미적용 이유 |
|---|---|---|
| `19719f4f2d` | 진단만 적용 | `power.py`와 `tools/jetlink/usb_power.py`의 empty-port debounce, USB_ICL zero-current vote와 helper 재확인 방식을 검토했다. 전원 구조가 불명확하므로 helper·전류 차단·role 변경은 가져오지 않았다. power/data role과 voter 노드만 읽는다. |
| `163c000e05` | 선별 적용 | 프로세스 메타데이터의 PID+starttime 식별과 종료 프로세스 캐시 삭제 원칙을 적용했다. XPlus의 기존 smaps 캐시도 함께 만료시킨다. 당근의 전체 SmapsSampler 교체나 proclogd 시작 시 heap freeze는 가져오지 않았다. |
| `2cd1d7978d` | 미적용 | XPlus는 modeld_local의 단일 ModelState를 사용하는 구조이며 해당 당근의 small_model standby 비교 worker 생성 구조가 없다. 기존 signal shadow를 새로 만들거나 제어 경로를 수정하지 않았다. |
| `2c479eaedf` | 미적용 | Wi-Fi HUD의 공유 USB 대기 우회는 Jetson USB-A 직결 HUD에 맞지 않는다. 기존 USB 처리를 유지한다. |
| `6da330c55f` | 선별 적용 | renderer 종료 후 GC unfreeze/threshold 복원과 exception handler가 끝난 뒤 세션 참조 회수를 적용했다. Wi-Fi 발견 절차와 native malloc_trim은 가져오지 않았다. |
| 최신 `fa553468ef` | 적용 | H.264 Annex-B Python 바이트 순회 대신 bytes.find를 사용하는 동등 검색만 반영했다. rate control, 해상도, 화면 구성, encoder 설정은 유지한다. |

## 실제 수정 내용

### 메모리 관리

- `openpilot/system/proclogd.py`: 종료 프로세스의 metadata/smaps 제거, 같은 이름의 PID 재사용 식별,
  RSS가 임계값 아래로 줄어든 프로세스의 불필요한 smaps 캐시 제거.
- `openpilot/selfdrive/carrot/cluster/cluster_profile.py`, `cluster/main.py`, `cluster_autorun.py`:
  세션마다 GC 설정 복원, 종료된 renderer의 참조 회수, 개별 close 오류가 다른 자원 정리를 막지 않도록 처리.
  프레임 처리 중 강제 GC, native heap trim, cache drop, 메모리 부족에 따른 임의 프로세스 종료는 없다.
- `openpilot/tools/camerastream/orin_yolo_direct.py`: 수신 배치 전체를 보관하는 대신 순서대로 한 패킷씩 처리한다.
  종료 경로에서 구독·결과 socket과 codec/model 참조를 정리한다.
  기존 `write_status(..., state=...)` 인수 충돌로 시작 단계에서 실패하던 결함도 수정했다.

### 연결·YOLO·HUD 복구

- `openpilot/tools/jetson/stream.py`: 1초 수신 timeout, 5초 무수신 후 구독 갱신, 재시도 횟수·마지막 오류 기록.
  설정한 hostname은 다시 해석하므로 DNS가 새 주소를 반환하면 따라갈 수 있다.
  고정 IP 자체가 바뀌었는데 새 주소 정보가 없는 경우에는 설정 갱신이 필요하다.
  임의 비콘의 IP를 자동 신뢰하는 검색 기능은 추가하지 않았다.
- direct YOLO는 전송 세대 변경 또는 encodeId 불연속 시 decoder를 재구성하고 새 keyframe/header를 기다린다.
  comma 재부팅·영상 재시작을 처리하며, 실패한 추론 process를 코드 안에서 중복 실행하지 않는다.
  실제 스크립트 진입점에는 nonblocking flock을 추가했다.
- 두 기존 YOLO systemd 설치 스크립트는 300초 안에 최대 3회 시작, 10초 재시작 간격을 설정한다.
  지속적인 GPU/시작 오류로 start limit에 도달하면 자동 재시작이 멈춘다.
  원인을 해결한 뒤 관리자가 `systemctl reset-failed nexo-yolo.service` 및 start로 복구해야 한다.
  이 작업에서는 설치 스크립트를 실제 장치에서 실행하지 않았다. 기존 설치에 반영하려면 별도 재설치가 필요하다.
- `openpilot/tools/jetson/host.py`: USB 우선권을 유지하면서 Wi-Fi 영상 정체·예외 시 기존 구독을
  정리하고 제한된 간격으로 다시 연결한다. source epoch와 keyframe gate를 재설정한다.
- `openpilot/tools/jetson/recovery.py`, `hud.py`: 기존 HUD 잠금을 유지한 한 process 안에서
  panel과 snapshot/onroad 조건이 준비될 때만 renderer를 시작한다. 세션 종료 시 상태 marker를
  만료시키고 참조를 회수한다. 짧은 실패는 2초, 4초, 이후 60초 대기로 제한한다.
  시도·연속 실패 횟수와 마지막 오류를 기록한다. 횟수는 HUD supervisor process 생애 기준이다.

### 기존 7000 진단 확장

- `openpilot/common/stability.py`: procfs의 MemTotal/MemAvailable 및 주요 process RSS를
  5초마다 관측한다. 최대 32개 process, 최대 61개 표본(통상 5분)만 유지하며
  전체 메모리 및 동일 PID+starttime process의 증가량을 제공한다. 명령줄은 전송하지 않는다.
  smaps 전체 순회, subprocess 실행, 새로운 daemon/server는 없다.
- `native_host.py`, `host.py`, `jetson_status_beacon.py`, `state.py`: 기존 전송 경로로
  Jetson 메모리·HUD 복구 상태를 전달한다. 비콘은 YOLO NRestarts/Result/ExecMainStatus도 제공한다.
  신뢰할 수 없는 원격 수치는 허용 필드/개수로 제한한다.
- `server/services/jetson.py`, `server/features/jetson_pages.py`: comma/Jetson 메모리,
  주요 process RSS, 증가량, 복구 횟수, 마지막 오류와 읽기 전용 USB 전원 관측을 표시한다.
  원격 상태 lease가 만료되면 메모리를 현재 상태로 표시하지 않는다.
  기존 통합 진단이 호출하는 diagnostic_summary에도 정보를 포함한다.

## 검증 결과와 한계

최종 Windows 관련 회귀: **189 passed, 5 skipped, 1 deselected**.

- 새 테스트: PID churn 2,000회 양쪽 캐시 상한, PID 재사용·RSS 감소·실제 procLog publication,
  메모리 관측 throttle/5분 상한, 읽기 전용 fake sysfs, 원격 진단 만료 및 필드 제한,
  단일 구독 소유권·주소 변경·재시도 간격, 실제 YOLO 루프의 comma frame counter 초기화와 GPU 실패 후 cleanup,
  HUD 연속 실패 backoff 및 exception 참조 회수, GC freeze/restore 400회,
  H.264 짧은 prefix 전 조합 및 830KB 이상 입력 동등성.
- 기존 테스트: USB/NEXD/JLNK framing, heartbeat/reconnect, HUD adapter, snapshot 제한,
  설치·관리·7000 API·CSRF·주행 중 관리 제한을 포함한다.
- 5개 skip: pinned Carrot server checkout 부재 1, pyray 부재 1, 빌드된 Linux cereal/msgq runtime 2,
  실제 Linux flock 1. Windows의 fake socket/GPU 테스트는 실제 TensorRT/화면/USB 검증이 아니다.
- 기존 protected-install rollback 테스트 1개는 Windows directory rename에서 WinError 5가 발생했다.
  이 파일은 수정하지 않았으며 해당 사례는 최종 Windows 묶음에서 제외했다. Linux에서 재검증해야 한다.
- C++ pytest 설정 플러그인 부재 및 기존 aiohttp AppKey/deprecation 경고 19개가 남아 있다.
  전체 openpilot/차량 제어 빌드·실차 회귀가 통과했다는 의미가 아니다.
- 변경 Python 21개 파일의 Python 3.10 grammar 검사, Python 구문 검사 통과.
  신규/주요 helper Ruff 통과, 변경 파일의 기존 NEXO 대비 추가 Ruff 진단 0건.
  7000 페이지 JavaScript 및 두 Bash 설치 스크립트 구문 검사 통과.

실행 환경은 작업 폴더의 Python 3.12, pytest 9.1.1, pytest-asyncio 1.4.0, aiohttp 3.14.5이다.
테스트는 root conftest의 Linux 장치 초기화를 제외한 `--noconftest` 환경이며
`PYTHONUTF8=1`, 명시적인 asyncio plugin/auto mode를 사용했다.

```text
python -m pytest -p pytest_asyncio.plugin -o asyncio_mode=auto -o addopts='' --noconftest \
  openpilot/tools/jetson/tests openpilot/system/tests/test_proclog_lifecycle.py \
  openpilot/selfdrive/carrot/server/tests/test_jetson_local_status.py \
  -q --tb=short -rs -k 'not test_service_start_failure_rolls_back_after_candidate_selection'
```

## 반드시 남아 있는 장치 검증

1. 장시간 실제 Jetson YOLO/TensorRT·영상·USB HUD 연결에서 RSS/PSS/CUDA/graphics 추이와 부하를 관측한다.
   이번 400회/2,000회 모의 시험은 장시간 실차 누수 해소 증명이 아니다.
2. USB 분리·재연결, frame 무수신, Jetson service restart, comma 재부팅,
   hostname DNS 변경과 고정 IP 갱신을 실제 구성에서 확인한다.
3. native encoder/ffmpeg worker 종료, panel reconnect 후 중복 owner 부재와 화면·주차센서·기어·방향지시등 표시 유지를 확인한다.
4. Linux 실제 flock, 빌드된 cereal/msgq/pyray, 고정 server와의 통합 및 제외된 protected rollback을 검증한다.
5. USB 전원 보호는 계속 **미적용**이다. 독립 전원·실제 USB 역할·HUD 전원·전류/전압을 검증하기 전 활성화하지 않는다.
   전류 차단이나 role 변경을 수행할 코드를 이번 변경에 넣지 않았다.

현재 장치에 설치·원격 재시작·USB 전원 변경·실차 테스트를 수행하지 않았다.
feature 브랜치의 검토 가능한 코드이며 실차 검증 완료 릴리스가 아니다.

Docs-Not-Needed: 설정 또는 사용자 가이드 변경이 없으며, 범위·근거·검증 한계를 이 개발 문서에 기록했다.
