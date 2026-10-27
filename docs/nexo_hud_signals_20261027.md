# NEXO 외부 HUD 신호등: 구현 범위와 장비 적용 안내

## 기준과 적용 범위

- 작업 전 NEXO: `49ade550d9c97c2b1be03b8f4f762bbbde77bfa0`.
- 비교한 당근 carrot-wip: `604bc0a22733e648ec05b268da1ffaae7db52a69`.
- 보호형 Jetson 번들의 기존 carrot-jetson 고정 버전: `d0dc96fa32410403dfb2fefff3e2909c11ec109b`. 이번 작업에서 바꾸지 않는다.
- `NEXO_HUD_SIGNALS`는 계속 기본 OFF. 콤마와 Jetson 실장비에 접속하거나 설치한 결과가 아니다.
- DM, MED, SCC, CAN/DBC, 레이더, 조향/가감속/정차 제어, 주차센서, 차량 설정은 변경하지 않는다. 이 기능에는 차량 메시지 발행이나 제어 파라미터 쓰기가 없다.

## 기존 구현과 당근 비교

기존 NEXO에는 표시 코드뿐 아니라 실제 카메라 관측 생산자가 있다.
`process_config.py`의 `signalcolord`가 `signal_color_shadow.main()`을 시작하고,
`enabled`와 `tracking_enabled`가 모두 1이면 `signal_tracking_shadow`가 ROAD VisionIPC 프레임을 읽는다.
`tools/signal_analysis/signal_tracker.py`의 OpenCV 관측기가 적색/녹색 후보와 트랙을 생성하고,
`signal_assist_runtime.publish_observation()`이 `/dev/shm/carrot_signal_observation.json`을 원자적으로 쓴다.
이 함수가 제어 모듈과 같은 파일에 있다는 이유로 제어 기능을 활성화하지 않는다. 기존 별도 `assist_enabled`는 변경하지 않는다.

`enabled`만 켜고 `tracking_enabled`를 켜지 않으면 ONNX 비교 모드로 들어갈 수 있으므로 관측 JSON 생성 조건을 만족했다고 볼 수 없다.
추적 관측 모드는 기존 OpenCV 알고리즘을 사용한다. 신호등용 새 ONNX/TensorRT 모델 설치는 필요하지 않다.
검출기는 제한된 가로형 신호등과 야간 광원 후보를 다루며 차로별 신호 적용성, 황색, 실제 도로 정확도는 보장하지 않는다.
실제 차량에서 프로세스가 실행 중인지, 신선한 관측 JSON이 존재하는지는 아직 확인하지 않았다.

| 항목 | 기존 NEXO | 비교한 당근 | 이번 NEXO 변경 |
|---|---|---|---|
| 관측 입력 | observation JSON | 별도 signal_display JSON, display_enabled | 기존 observation 생산자 재사용 |
| 전송 | age_s와 정규화한 점만 전달 | 프레임/세션/시각/박스와 4KiB 한도 | 식별 정보를 보존한 version 2 선택 필드 |
| 색 | 트랙 확정 상태와 raw가 일치해야 색 표시 | 현재 raw, 별도 연속 표시 | 현재 raw만 적/녹; 미확정은 회색 물음표 |
| 프레임 검사 | 수신 측 프레임/세션 대조 없음 | 카메라 식별 정보와 유효시간 대조 | ROAD 프레임 ID, 캡처 시각, 원본 크기 일치 필수 |
| 연속성 | age_s 기반 | 최대 2초 상태 유지 가능 | 이전 색 보간/유지 없음; 동일 관측 수명 연장 금지 |
| 위치 | 별도 CAMERA 2D 영역 | video_dest 투영 가능 | 기존 2D 영역 유지, 선택 가능한 아이콘 행 |

## 전달 및 표시 규칙

```text
콤마 ROAD VisionIPC
  → 기존 SignalTracker → carrot_signal_observation.json
  → SnapshotBuilder + 같은 프레임의 ROAD JPEG 미리보기
  → 기존 DisplayPublisher / Msg.HUD / USB-NCM TCP 연결
  → Jetson validate_snapshot → /dev/shm/nexo-jetson/hud.json
  → HUD 인스턴스별 RemoteSignals → 기존 NEXO 렌더러의 선택적 표시
```

별도 USB 연결/메시지 종류/전송 루프를 추가하지 않는다. HUD 주기 0.1초와 기존 TAIL_BUDGET 0.012초를 유지한다.
관측 원본 읽기는 32KiB, 정리한 신호등 레코드는 4KiB, 트랙은 20개까지다.
원본과 ROAD 미리보기의 frame ID 및 캡처 시각(오차 1µs 미만), 1344×760 원본 크기가 맞아야 표시한다.
ROAD JPEG 변환 전후 VisionIPC 버퍼의 frame ID도 확인한다.
관측 유효시간은 캡처 기준 송신 시 나이와 수신 후 경과를 합산해 0.35초 미만이다.
서로 다른 장비의 monotonic 값을 직접 비교하지 않고 나이를 수신 시계로 옮긴다.
최초 전송의 절대 편도 지연은 이 방법만으로 측정할 수 없으므로 실제 NCM 지연 검증은 별도로 필요하다.
동일 관측을 다시 받아도 마감 시각을 연장하지 않으며, 같은 관측 ID로 색/박스가 바뀌거나 역순으로 들어오면 숨긴다.
카메라/관측 세션 재시작은 새로운 캡처 시각과 유효 프레임으로 복구한다. HUD 연결 세션 재시작도 구분한다.
인식 중단, 카메라 누락/불일치, 오래된 스냅샷, 연결 단절은 신호등을 제거한다.

신호등 선택 필드 오류는 빈 표시로 정리한다. 패킷 공간이 부족하면 신호등을 기본 속도/내비/미리보기보다 먼저 버린다.
주변 객체 읽기 오류와 신호등 읽기 오류를 각각 격리하고, 기본 HUD 렌더링은 계속 수행한다.
기존 연결 자동 복구, TCP 승인 응답, 장시간 HUD 로그 코드는 그대로 사용한다.

- `NEXO_HUD_SIGNALS=1`: 신호등 표시 요청. 미설정 또는 다른 값이면 OFF.
- `NEXO_HUD_SIGNAL_VIEW=inset` 또는 미설정: 기존 CAMERA 2D 영역에 정규화한 박스 중심 표시.
- `NEXO_HUD_SIGNAL_VIEW=icon`: 왼쪽부터 최대 4개 작은 아이콘. 추가 관측 수는 `+N`으로 표시. 객체 표시도 켜져 있으면 같은 영역을 사용한다.
- 적색/녹색은 현재 인식 raw 색, unknown은 회색 물음표다. 미검출/예측만 남은 트랙은 표시하지 않는다. 황색을 만들어내지 않는다.
- `NEXO_HUD_OBJECTS`는 독립적이다. 기존 자동차/보행자/오토바이 표시를 유지한다.

2D 위치는 원본 영상 좌표계의 상대 위치다. 렌즈/크롭/회전/미러/화면 투영을 검증한 실제 영상 위 AR 박스가 아니다.
HUD 속도계/내비/경고 레이어는 기존 순서를 유지한다. 모든 테마·회전·패널 크기의 물리 가독성은 장비에서 확인해야 한다.
관측기와 미리보기는 독립적으로 샘플링하므로 정확히 같은 프레임을 못 만나는 경우 표시가 비거나 깜박일 수 있다.
이 경우 프레임 검사를 완화하지 말고 실제 두 스트림의 frame ID/시각과 생성 주기를 수집한다.

## 업데이트 대상

| 장치 | 필요한 파일/부분 |
|---|---|
| 콤마4 | `openpilot/tools/jetson/{camera,snapshot,hud_observations,hud_signals}.py`, `openpilot/selfdrive/modeld/jetlink/display.py` |
| Jetson NEXO 보호형 릴리스 | `openpilot/tools/jetson/{hud_observations,hud_signals,snapshot}.py` 및 동일 커밋 번들의 의존 소스 |
| 그대로 유지 | 기존 USB-NCM 구성, 자동 복구, 장시간 로거, `hud.py`/`ncm_display.py` 진입 경로, 기존 모델 SHA와 TensorRT, vendor 고정 커밋 |
| 이전 당근 프로그램만 설치된 Jetson | 먼저 보호형 설치 계약/서비스 경로를 검사해야 함. 콤마 업데이트만으로 구형 당근 HUD에 이 NEXO 표시가 생기지 않음 |

두 장치가 서로 다른 표시 스키마 버전이면 신호등이 숨겨질 수 있다. 기본 HUD 데이터 포맷은 유지한다.
개별 파일을 실행 중인 보호형 `current`에 덮어쓰지 말고 커밋된 릴리스 번들로 적용한다.

## 보호형 Jetson 사전 확인과 업데이트

다음 명령은 안내이며 이 작업에서 실제 장비에 실행하지 않았다. 차량을 안전하게 정차하고 offroad 상태에서 실시한다.

```sh
readlink -f /opt/carrot-jetlink/current
systemctl show carrot-jetlink.service carrot-jetlink-hud.service -p ExecStart -p ActiveState
systemctl show carrot-jetlink-update-apply.service -p ExecStart
cat /opt/carrot-jetlink/updates/nexo-ncm.json
cat /opt/carrot-jetlink/current/NEXO_RUNTIME.json
/opt/carrot-jetlink/venv/bin/python -c 'import sys,tensorrt; print(sys.version, tensorrt.__version__)'
```

기존 설치기는 `/opt/carrot-jetlink/current/tools/jetlink/{server,hud}.py`, 전용 venv,
DATA 저장소, 기존 updater 복구 진입점, Python 3.10/TensorRT 10.3.0, 서비스 활성 상태,
모델 SHA, 디스크 여유 등을 검사한다. 조건이 다르면 중단하는 검사를 우회하지 않는다.
NCM 인터페이스명과 `display_only`는 현재 설정에서 읽고 그대로 유지한다. 이 변경을 위해 추론 모드를 바꾸지 않는다.

깨끗한 검증 커밋의 저장소에서 기존 도구로 번들을 만들 수 있다.

```sh
python -m openpilot.tools.jetson.build_protected_bundle --carrot /path/to/pinned-carrot-jetson --compact /path/to/nexo-hud-signals.tar.gz
```

생성된 JSON의 source_commit/carrot_commit/model SHA 및 tar.gz SHA256을 검증한 뒤 전달한다.
이 빌더는 기존 SHA256/매니페스트 검증용 번들을 만든다. 새 디지털 서명을 발급하는 기능은 아니다.
기존 Carrot 서명 키/검증 코드/복구 코드는 수정하지 않았다. 서명 정책이 있는 장비에서는 그 정책에 따른 패키징도 필요하다.

검증된 번들과 그 안의 동일 커밋 `protected_install.py`/`protected_ncm.py`를 스테이징한 후,
기존 인터페이스와 모델 SHA를 명시해 기존 설치기를 사용한다. 아래 대문자 값은 실제 검사 결과로 치환해야 한다.

```sh
sudo /usr/bin/python3 /path/to/staged/openpilot/tools/jetson/protected_install.py install \
  --bundle /path/to/nexo-hud-signals.tar.gz --sha256 VERIFIED_BUNDLE_SHA256 \
  --ncm-interface EXISTING_USB_INTERFACE --model-sha256 EXISTING_COMMA_MODEL_SHA256 --confirmed-offroad
```

기존 설정이 `display_only=true`일 때만 동일 명령에 `--display-only`를 추가한다.
기존 추론 설치에서는 기존 설치기의 모델 검증/기억 절차가 실행되지만 이번 번들의 모델 내용과 런타임 버전은 바꾸지 않는다.
설치기가 성공해도 실차 신호등 인식/물리 HUD 출력을 검증했다는 뜻은 아니다.

## 실차 활성화와 확인

1. 기본 OFF 상태에서 기존 HUD 속도/내비/자동 복구가 정상인지 먼저 확인한다.
2. 콤마에서 `pgrep -af signal_color_shadow`와 관측 JSON의 timestamp/session/frame_id 변화 여부를 확인한다.
   `/data/signal-color-shadow/enabled`와 `tracking_enabled`가 모두 1이어야 추적 모드가 실행된다.
   기존 `assist_enabled`가 1이면 이 관측을 기존 제어 기능도 소비할 수 있다. 이번 작업에서는 그 설정을 변경하지 않는다.
   해당 경우에는 표시 전용 조건이 성립하지 않으므로 새 관측기를 활성화하지 않는다.
3. 별도 제어 소비가 꺼져 있고 정차 상태에서 관측기 시험을 하려는 경우에만, 기존 값을 백업한 다음 `enabled`/`tracking_enabled`를 1로 설정한다.
   이 작업은 자동 배포/번들 설치 시 실행되지 않는다. 매니저가 started 조건을 만족해야 `signalcolord`를 실행한다.
   JSON 파일의 존재만 보지 말고 적/녹 raw, 현재 age=0 트랙, 증가하는 frame_id, 신선한 캡처 시각을 확인한다.
4. Jetson `/dev/shm/nexo-jetson/hud.json`에서 `signal_display.version=2`, record, cameras.road의 frame/time/source 크기를 확인한다.
   record=null이면 미검출뿐 아니라 프레임 불일치/만료/잘못된 데이터일 수 있다. 예시 데이터를 장비 파일에 주입하지 않는다.
5. 시험용으로 Jetson RAM drop-in을 만들면 다음 부팅에는 기본 OFF로 돌아간다. 설치가 완료되고 실제 서비스 경로를 확인한 뒤에만 사용한다.

```sh
sudo mkdir -p /run/systemd/system/carrot-jetlink-hud.service.d
printf '[Service]\nEnvironment=NEXO_HUD_SIGNALS=1\nEnvironment=NEXO_HUD_SIGNAL_VIEW=inset\n' \
  | sudo tee /run/systemd/system/carrot-jetlink-hud.service.d/90-nexo-signals.conf
sudo systemctl daemon-reload
sudo systemctl restart carrot-jetlink-hud.service
```

아이콘 모드를 시험하려면 위 파일에서 `inset`을 `icon`으로 바꾼다.
시험 종료 시 자신이 만든 `90-nexo-signals.conf`만 제거하고 daemon-reload 및 HUD 서비스 재시작을 수행한다.
관측기 플래그는 시험 전 저장한 값으로 복원한다. DM/차량 제어 설정은 건드리지 않는다.

6. 실제 적→녹, 미검출, 역광/야간, 여러 신호등, 차량/보행자 동시 표시를 기록한다.
7. 정차 시험 환경에서 카메라/인식 중단, NCM 단절/복구, HUD 프로세스 재시작 후 낡은 색이 사라지고 기본 HUD가 복구되는지 확인한다.
8. `/data/media/nexo-long-logs/hud_link_samples.jsonl`, `hud_link_events.jsonl`,
   7000 서버 `/view/nexo-long-log-report.txt`, `/download/nexo-long-log-latest.tar.gz`를 사용한다.
   신호등 on/off 비교 시 CPU/프레임 지연/대기 화면 전환 시각도 함께 수집한다.

## 검증 범위와 복구

자동 검증에는 신호등 스키마/색/프레임/세션/수명, 예외 격리, 패킷 예산,
합성 RGB→기존 검출기→실제 JSON writer→HUD 처리, 실제 localhost TCP 전송/승인/재연결,
실제 UDP 상태 수신, 기존 소프트웨어 FFmpeg 인코딩, HUD 자동 복구/장시간 로그 회귀가 포함된다.
Linux Actions에서 전체 빌드 및 기존 안전/통합/표시 테스트를 통과한 커밋만 NEXO로 병합한다.
최종 실행 수와 SHA는 해당 PR/Actions 산출물 및 별도 완료 보고서에서 확인한다.

미검증: 실제 콤마4 캡처/CPU, 실도로 오검출·미검출, 물리 USB-NCM 지연/장시간 단절,
Jetson GPU/EGL 전체 화면, 실제 HUD 패널/테마별 가독성, 보호형 설치 및 재부팅 복구.
일반 관측기 테스트나 합성 이미지 통과를 실제 도로 인식 성공으로 해석하지 않는다.
실제 끊김 로그가 없으므로 간헐적인 연결 대기 화면의 장비 원인을 확정하지 않는다.

기능 해제는 HUD의 `NEXO_HUD_SIGNALS` 환경 설정 제거만으로 가능하다.
코드 복구 기준은 `49ade550d9c97c2b1be03b8f4f762bbbde77bfa0`이며, 공유 NEXO 이력을 강제로 되돌리지 말고 이번 병합 커밋을 `git revert -m 1 MERGE_SHA`로 되돌려 CI를 통과시킨다.
Jetson 릴리스 복구는 기존 설치기의 `rollback --confirmed-offroad`와 저장된 이전 릴리스/모델 메타데이터를 사용한다.
복구 후에도 기본 HUD와 기존 서비스/모델 SHA를 확인한다.
