# 외부 HUD 젯슨 AI 상태 표시

## 2026-11-14 보완

기존 `jetlink_ai_indicator.py`와 HUD snapshot 경로를 확장합니다. 새 모델 실행기,
엔진 빌더, 추론 전환, 차량 설치/활성화는 추가하지 않습니다. 실험용 엔진 결과는
이 변경으로 주행 모델이나 조향·가속·제동에 전달되지 않습니다.

| HUD 표시 | 읽기 전용 증거 |
| --- | --- |
| `JETSON 연결 끊김` | 최신 peer 교환 증거 없음 |
| `JETSON 연결 · AI 대기` | 최신 USB/NCM peer 교환은 있으나 엔진 준비 증거 없음 |
| `JETSON · AI 실험 대기` | 기존 native ready 또는 companion의 명시적인 model_ready. 실제 추론 증거 없음/만료 |
| `JETSON AI ON` | 아래 완료된 native 추론 증거가 모두 최신일 때만 표시 |

ON은 다음 조건을 모두 요구합니다.

- 콤마4의 `/dev/shm/nexo-jetlink.json`이 오류 없이 ready이고, peer 교환 및
  **실제 modeld 요청**의 `last_infer_monotonic`이 2.25초 이내입니다.
  daemon의 합성 warmup은 이 필드를 갱신하지 않습니다.
- `/dev/shm/nexo-jetlink-model.json`이 2.25초 이내이고 오류가 없으며,
  `active`, `ready`, `external_inference_verified`가 엄격한 boolean true입니다.
- 모델 이름은 기존 `NEXO native driving_supercombo`이고, 두 상태 파일의
  유효한 SHA-256이 일치합니다.
- `completed_inferences`가 양의 정수이며 **마지막 완료 시각**
  `last_inference_at_mono`가 2.25초 이내입니다. 상태 파일 갱신만으로는
  멈춘 추론을 활성 상태로 연장하지 않습니다.

엔진 파일 존재, 약 55MB 크기, TensorRT 10.3 빌드, GPU 테스트 PASSED,
평균 약 2.93ms라는 과거 실험 결과만으로 ON을 표시하지 않습니다.
별도로 생성한 실험용 엔진 파일을 탐색하거나 원본 모델 계약에 등록하지도 않습니다.
준비 증거를 게시하지 않는 오프라인 실험 파일의 준비 상태는 추측하지 않습니다.
앞차 예측·주행 계획의 수치 차이가 있는 실험 엔진의 차량 제어 적합성은
검증되지 않았습니다. ON도 계산 수행의 관찰 표시이며 제어 적합성 승인이 아닙니다.

## 전송·만료·화면 유지

`SnapshotBuilder`는 기존 HUD JSON에 선택적 `jetlink_ai` 객체를 추가합니다.
연결·준비·추론 boolean, 남은 추론 lease, 실제 측정값만 포함합니다.
구형 수신기용 `jetlink_inference_active` boolean도 더 엄격한 판정으로 유지합니다.
새 수신기는 구형 boolean만으로 ON을 표시하지 않습니다.
잘못된 선택 필드는 제거하거나 비활성으로 내리며 차량 snapshot 자체를 폐기하지 않습니다.

콤마4에서 각 증거의 남은 lease 중 가장 짧은 값을 계산하고, 젯슨에서는
자신의 수신 후 경과시간을 차감합니다. 서로 다른 호스트의 monotonic 시계를
직접 비교하지 않습니다. 기존 0.5초 HUD snapshot 신선도 제한도 유지합니다.
전송 시간은 이 방식으로 직접 계측할 수 없으므로 표시 만료에는 짧은 전송 지연이
더해질 수 있습니다. snapshot이 끊기면 기존 대기 화면 처리로 ON도 사라집니다.

유효한 추론 응답이 있을 때 `GPU …ms`(실행 us를 ms로 변환), `추론 RTT …ms`
(modeld에서 응답 완료까지의 왕복시간)를 표시합니다. HUD-only NCM 경로에서는
기존 `state_rtt_ms`를 `통신 RTT …ms`로 표시합니다. 추론 RTT는 GPU·준비·전송을
포함하므로 순수 네트워크 지연이나 편도 지연으로 해석하지 않습니다.
값이 없거나 만료/오류이면 숫자를 추측하지 않습니다.

기존 P/R/N/D 렌더러를 먼저 호출한 뒤 작은 배지를 덧그립니다. P단의 수소 계기
제목과 겹치지 않도록 위치를 줄입니다. 차량 그림, 기어 선택, 카메라, SPAS12,
속도·각종 계기, 패널 재연결, 인코더, USB/NCM 송수신 루프는 변경하지 않습니다.
추가 파일 읽기는 콤마4의 기존 분리된 HUD snapshot worker에서만 수행합니다.

## 검증 범위

`openpilot/tools/jetson/tests/test_jetlink_ai_indicator.py`는 상태 파일,
마지막 완료 시각/peer 만료, 재연결 대기, 모델 불일치, 잘못된 타입/숫자,
측정 단위, 선택 필드 보존/제거, NCM relay, P/R/N/D 렌더러 호출과 배지를
모의 입력으로 검증합니다. 기존 HUD/기어/카메라/신호/NCM 회귀 테스트도 함께 실행합니다.
이 자동 테스트는 실차 GPU 실행이나 실제 패널 픽셀 출력을 검증하지 않습니다.

GitHub 푸시는 차량에 설치하지 않습니다. 별도의 실차 설치/활성화는 수행하지 않습니다.
