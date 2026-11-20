# 엑스플러스 AI 기준: 당근 N16 / 2019 넥쏘 (2026-11-20)

이 저장소의 다음 AI 개발은 별도의 신규 주행 모델 탐색/도입을 기본 방향으로 삼지 않는다.
공개 `ajouatom/openpilot`의 당근 Jetlink N16 모델을 고정된 참조로 삼아 넥쏘 2019년형에 맞춘다.
변경 시마다 당근 최신 소스와 모델 규격을 비교하되 새 모델로 무조건 자동 전환하지 않는다.

## 1단계 — 소스/계약 등록 완료 범위

- 당근 공개 `carrot-wip`의 `openpilot/selfdrive/modeld/jetlink/jetson_model.json` 원문을 `openpilot/tools/jetson/carrot_n16_spec.json`로 별도 보관했다.
- 참조한 Git blob SHA는 `22cd8351ea1596f85c1bca7e75d0f979e87d98ae`; ONNX SHA-256은 `01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`, 크기는 `814591107`바이트다.
- `nexo_n16_profile.json`에서 차량 대상을 `Hyundai NEXO (FE), 2019`로 명시하고 P/D/R HUD, SPAS12 수신 전용 센서, Comma4 기존 카메라 입력과 내비게이션 상태의 기존 경로를 보존하도록 기록한다.
- 당근 N16은 `new_img`, `nav_map`, `navigation` 및 장기 recurrent state를 받는다. 현재 NEXO 원본 모델의 `img`, `big_img`, `features_buffer`와 직접 호환되지 않는다.
- 당근 N16 출력 `[1,18580]` 및 각 출력 slice는 기존 넥쏘 모델의 `[1,2576]`과 별도로 해석해야 한다. NEXO 전용 차선, 앞차, 경로의 HUD 픽셀 좌표 및 실제 차량 geometry는 아직 검증되지 않았다.
- 표준 라이브러리만 쓰는 `nexo_n16_registry.py`가 파일 없을 때는 등록 상태만 보고한다. `--verify-model /path/to/official.onnx` 선택 시 **사용자가 이미 가진 파일**에 한해 정확한 크기/SHA를 스트리밍으로 확인한다.
- 파일을 다운로드, 복사, 캐시 설치, 서비스 활성화, 엔진 생성 또는 추론하지 않는다. 실제 Jetson 설치 여부는 반드시 `false`로 보고한다.

## 안전 및 향후 범위

1단계는 소스 코드에 **비활성 시험 계약**만 등록한 것이다.
`modeld`, JoiningModel, 카메라 장치 소유권, 현재 ONNX/엔진 캐시, Jetson USB/NCM, HUD 렌더링, NEXO CAN/DBC, 차량 제어 조건을 변경하지 않았다.
기존 주행 모델은 사용자가 현재 사용하는 상태 그대로 유지된다.
실차 차량 이력이나 정확도 시험을 근거 없이 통과로 표시하지 않는다.

2단계에서만 기존 검증된 정비 절차로 OFFROAD 진입을 확인한 뒤 N16 TensorRT 엔진의 별도 GPU 시험을 고려한다.
GPU 메모리, 반복 상태, ONNX CPU 대비 출력·정밀도, 성능과 열·전원·USB/HUD 간섭을 통과해야 한다.
원본 파일 교체나 modeld/controls 연결은 하지 않는다.

3단계 HUD 관찰 기능은 실제 영상 입력 warp, 모델 해석, frame freshness와 넥쏘 2019년형의 기어·카메라 구조를 별도로 확인해야 한다.
P/R/D 상태, 후방 화면, 수소 잔량, SPAS12 주차센서, 후측방 경고를 유지한다.
N16 예측은 YOLO 객체 상자와 다르므로 임의의 박스나 차선을 만들어 표시하지 않는다.
주행 데이터가 들어오더라도 control/steer/accelerate/brake/CAN 등에 대한 출력 경로는 없어야 한다.

표준 조회 명령:

```sh
python3 -m openpilot.tools.jetson.nexo_n16_registry
python3 -m openpilot.tools.jetson.nexo_n16_registry --verify-model /path/to/official-big_driving_supercombo.onnx
```

둘 다 **읽기 전용**이다. 모델 파일이 없으면 첫 번째 명령만으로 1단계 등록을 확인한다.
젯슨에 실제로 설치했다거나 외부 HUD에서 N16 AI가 돌고 있다는 의미는 아니다.
