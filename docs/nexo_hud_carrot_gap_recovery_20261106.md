# 외부 HUD 순간 검은 화면 — Carrot 비교 및 보수적 완화 (2026-11-06)

## 현장 증거
사용자가 제공한 2026-11-06 08:07–08:10 진단은 NCM/TCP `connected`, HUD renderer `streaming`, systemd 재시작 0인 반면 `hud_receive_delayed`, `snapshot_expired`가 발생했고, `encoded_expired_frames`가 210→234→262로 증가했습니다. 이 수치는 물리 패널 검게 꺼진 횟수가 아닙니다. `physical_hud_verified=false`이므로 전원·USB 패널·디코더의 검은 출력 원인을 입증하지 않습니다.

## 당근 방식 검토
- [Carrot `tools/jetlink/hud.py`](https://github.com/fallen8angel/openpilot_Carrot/blob/carrot-wip/tools/jetlink/hud.py)는 Orin Nano에서 10 FPS libx264 인코더 및 `--usb-h264-ffmpeg-threads 1`을 명시합니다. HUD 렌더러는 젯슨의 표시 기능이며 차량 제어와 무관합니다.
- [Carrot `tools/jetlink/hud_protocol.py`](https://github.com/fallen8angel/openpilot_Carrot/blob/carrot-wip/tools/jetlink/hud_protocol.py)는 표시용 snapshot 신선도 만료를 보존하고, 별도 표시 송신자와 기능을 분리합니다.
- [Carrot `tools/jetlink/host_usb.py`](https://github.com/fallen8angel/openpilot_Carrot/blob/carrot-wip/tools/jetlink/host_usb.py)는 USB Bulk 수신 요청을 유지하는 복구책입니다. **NEXO의 USB-NCM/TCP HUD 전송에 직접 적용할 수 없으므로 이 커밋에는 이식하지 않습니다.**

## NEXO HUD만 변경
1. 젯슨 화면의 FFmpeg 인코더/필터 스레드를 자동이 아닌 **1개**로 제한합니다. Carrot과 동일한 보수적 설정이며 실제 젯슨 CPU 개선은 실험으로 재확인해야 합니다.
2. NEXO에는 소프트웨어 H.264 인코더의 독립 IDR 출력에 대한 **0.5초 만료 검사**가 별도로 있습니다. 아직 유효한 차량 데이터라도 남은 유효시간이 **0.2초 이하**이면 이미 노후해질 가능성이 크므로, 렌더링 후 **차량 정보 없는 눈에 보이는 대기 프레임**으로 교체한 다음 인코딩합니다. 이미 만료된 프레임을 오래 표시하거나 H.264 부분 청크를 버리지 않습니다.
3. `hud-input.json` 기록에서 `encoder_lease_short / preencode_waiting`과 통신 지연 `hud_receive_delayed / snapshot_expired`를 분리합니다. `hud-status.json`에 `preencode_reserve_waits`, `encode_lease_remaining_s`를 보고하여 추후 원인 분석에 사용합니다.
4. 원래의 0.3초 HUD 전송 제한, 0.5초 원본 데이터 신선도 제한, 0.5초 인코더 정지 검출, 10초 HUD supervisor 정지 검출, USB 패널 소유권과 재시작 정책은 바꾸지 않습니다.
5. 모델, DM, Panda, CAN, 차량 조향·제동·크루즈 제어, NCM 인터페이스/IP, 젯슨 기본 모델 경로는 변경하지 않습니다.

## 기대 효과 및 검증 범위
- **기대**: 시간 부족으로 뒤늦게 폐기할 차량 영상 대신 유효한 안전 대기 프레임을 인코딩해 패널 영상 공백을 줄이고, FFmpeg 스레드 과점유를 억제합니다. 대기 화면이 보이는 경우가 증가할 수 있습니다.
- **검증**: 새로운 빠른 단위 테스트에서 임박한 시각을 가정한 대기 프레임과 다음 차량 프레임 복구를 확인합니다. 기존 H.264 인코딩/디코딩, USB/TCP 만료와 안전 정책 회귀 테스트를 유지합니다.
- **한계**: HID/USB 전원이나 패널 디코더가 실제로 1초 미만 검게 꺼졌다가 켜지는 원인은 해당 로그만으로 확정되지 않았습니다. 실물 USB 패널에서 전·후 장시간 비교해야 합니다.

## 배포
`openpilot/tools/jetson/hud.py`는 **젯슨에서 실행되는 표시 엔진**입니다. 새 **서명된/검증된** NEXO HUD 패키지가 만들어지고 이를 7000 관리 페이지에서 정차·P단 상태에 설치해야 물리 젯슨에 적용됩니다. GitHub에 merge만 해서는 기기에 설치되지 않습니다. 콤마4의 사용자 인터페이스/주행 코드에는 변경이 필요하지 않으나, 관리 화면에서 새 release를 조회하려면 업데이트된 repository/검증 패키지가 필요할 수 있습니다. 새 패키지가 없으면 기존 패키지를 다시 설치하지 않습니다.
