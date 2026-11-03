# HUD 연결 끊김 30분 관측: NCM USB / 패널 USB / TCP / 인코더

작성: 2026-11-02. 구현은 읽기 전용이며 **실차 시험, Jetson 설치 및 물리 HUD 표시 확인은 수행하지 않았습니다.**

## 목적과 기존 진단과의 차이

- NCM USB(콤마↔Jetson 통신)와 패널 USB(Jetson↔TURZX 표시 장치)는 서로 다른 경로입니다.
- 연결 상태의 `last_transport_error`는 과거 오류 이력일 수 있습니다. 현재 상태와 마지막 갱신 시각을 먼저 확인합니다.
- `hud-status.json`의 성공적 USB 쓰기는 실제 LCD 픽셀 표시 성공을 증명하지 못합니다.
- 임의 TCP 요청, 테스트 영상 주입, USB 리셋, gadget 역할 변경, 서비스 재시작, CAN 송신은 하지 않습니다.

## 콤마에서 읽기 전용 30분 수집

정차 상태에서 NEXO 코드가 실행 가능한 환경의 해당 모듈을 사용합니다.

```sh
cd /data/openpilot
PYTHONPATH=/data/openpilot python3 -m openpilot.tools.jetson.hud_link_soak \
  --role comma --ncm-iface usb0 --seconds 1800 --interval 1 \
  --output /data/media/nexo-long-logs/hud-link-20261102-comma
```

콤마 출력은 `hud-link-samples.jsonl`, `hud-link-summary.json`입니다. 별도의 기존 장시간 로그 수집·압축 기능을 수정하지 않았으므로 이 폴더가 **자동으로** `/download/nexo-long-log-latest.tar.gz` 안에 들어간다고 가정하지 마십시오.

## Jetson에서 읽기 전용 30분 수집

Jetson에 실제로 해당 소스가 존재하고 Python으로 임포트 가능한 경로에서 실행합니다.
NCM 인터페이스 명칭은 Jetson에서 확인한 경우에만 `--ncm-iface <이름>`으로 지정합니다.

```sh
python3 -m openpilot.tools.jetson.hud_link_soak \
  --role jetson --seconds 1800 --interval 1 \
  --output /var/tmp/nexo-hud-link-20261102-jetson
```

패널 인식은 sysfs의 USB VID:PID `1cbe:0092` 또는 `1cbe:0123`만 확인합니다.
다른 기종의 패널은 식별되지 않을 수 있습니다. `panel_usb.status=unknown`은 고장 판정이 아닙니다.

## 별도 로컬 TCP 및 소프트웨어 인코더 검증

기존 `bench_hud_local.py`는 `--seconds 1800`으로 30분 가상 localhost TCP 세션을 실행할 수 있습니다.
인코더 항목은 짧은 소프트웨어 H.264 벤치마크입니다. 인코더를 30분 내내 연속으로 구동하는 시험이 아닙니다.
CARROT_JETSON 경로와 psutil/ffmpeg 등 기존 의존성이 있어야 합니다.

```sh
CARROT_JETSON=/path/to/pinned/carrot-jetson \
python3 -m openpilot.tools.jetson.bench_hud_local \
  --seconds 1800 --output /tmp/nexo-local-tcp-1800.json
```

이 벤치마크의 TCP 성공을 콤마↔Jetson의 물리 NCM 성공 또는 TURZX 화면 확인으로 보고하지 않습니다.

## 결과 해석

`ncm_usb_link_loss_possible`은 NCM 링크의 현재 carrier 상실과 TCP 비연결이 동시에 보일 때만 표시합니다.
`tcp_session_not_ready`는 최근 상태가 연결/HELLO 완료가 아닐 때 표시합니다.
`panel_usb_not_enumerated`는 렌더러 실행 증거가 있으나 알려진 패널 VID:PID가 잡히지 않을 때 표시합니다.
`encoder_failure_possible`은 현재 복구 상태가 비실행이고 인코더 실패 유형이 기록된 경우에만 표시합니다.
`panel_write_recent_not_physical_proof`는 패널 쓰기가 최근인 상태를 뜻하며 실제 화면 표시 성공 판정이 아닙니다.
`insufficient_evidence`는 자료가 없거나 오래되어 원인을 특정할 수 없음을 뜻합니다.

콤마의 `time.monotonic()` 값과 Jetson의 `time.monotonic()` 값은 서로 직접 빼지 않습니다.
두 장비의 출력 JSONL은 벽시계(UTC) 시각을 참고하여 사건 순서를 비교하되 장비 시계 보정 오차를 고려해야 합니다.

검증 조건: Python 테스트 `openpilot/tools/jetson/tests/test_hud_link_soak.py`.
실제 30분 데이터, 렌더러 장시간 부하, 패널 USB 뽑힘·재연결, 주행 중 화면 가시성은 별도 정차/실기 검증이 필요합니다.
차량 제어/운전자 감시/레이더/모델/CAN/DBC 및 기존 장시간 로그 배포 경로는 변경하지 않았습니다.

## 2026-11-03: 자동 복귀하는 검은 화면의 추적

11월 3일 차량 로그의 `snapshot_expired / hud_receive_delayed`와 증가하는 `encoded_expired_frames`는 **입력 지연의 증거**이지 검은 LCD 픽셀이나 패널 전원 단절의 증거가 아닙니다. 정상으로 복구된 샘플에서 TCP 연결과 패널 전송 보고가 동시에 정상일 수 있습니다.

- `hud.py`의 느린 캡처(500ms 이상)는 기존의 불투명 검정 프레임 대신 기존 `WAITING FOR VEHICLE DATA` 안내 프레임을 출력합니다. 사용자 화면이 검정으로 변하는 하나의 소프트웨어 경로를 제거하지만, USB 패널/백라이트 물리 불량이나 인코더 중단을 고쳤다고 주장하지 않습니다. 오래된 차량 속도나 객체 영상은 전송하지 않습니다.
- `hud_link_soak.py`는 `hud-input.json`의 마지막 데이터 간격과 경과 시간, `render_expired`, 유효한 소프트웨어 패널 쓰기 시각, 패널 VID:PID, 연속 샘플에서의 폐기 프레임 증가량을 기록합니다. `panel_write_stalled_possible`은 **소프트웨어상의 출력 지연 의심**일 뿐 모니터 화면의 실측이 아닙니다. 과거 `last_gap`은 발생 시각과 함께 해석해야 하며 현재 오류로 오인하지 않습니다.
- 진단 시에는 차량을 정차시킨 다음 양 장치에서 기존 명령을 `--interval 0.2 --seconds 1800`으로 실행할 수 있습니다. 나중에 실제 화면이 검게 변한 시각(한국시간)과 JSONL의 `observed_at_utc`를 함께 대조하십시오. 통신과 인코더 모두 정상으로 보이는데 실제 모니터만 검게 변한다면 패널 USB 인식 변화, 전원·백라이트·케이블, USB 비디오 디코더 및 실제 광학 화면을 별도로 검사해야 합니다.
- 화면이 복귀했다는 사실만으로 프로그램이 재시작되었다고 단정하지 않습니다. 이번 수정은 **진단과 HUD 화면 표시에만** 해당합니다. 운전자 감시, Panda, CAN, 차량 제어, 추론 모델 및 USB 리셋 정책은 변경하지 않습니다. 실차 재현/실기 패널 출력 시험, Jetson의 보호형 DATA 업데이트, 실제 차량 설치는 수행하지 않았습니다.
