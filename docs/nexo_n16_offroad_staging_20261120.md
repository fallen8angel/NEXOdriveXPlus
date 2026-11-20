# 당근 N16 모델: 집에서 별도 보관 및 점검 (2019 넥쏘)

이 도구는 **차량 제어용 설치 프로그램이 아닙니다.** 사용자가 이미 정식으로 확보한 당근 N16 ONNX 파일을
젯슨의 격리된 연구용 캐시에 복사하고 정확한 SHA256을 확인합니다.
기존 차량 주행 모델·Comma4 modeld·CAN·HUD·기존 TensorRT 엔진·서비스는 바꾸지 않습니다.

## 실제 파일 정보
- 당근 `carrot-wip` 공개 JSON 참조의 N16 ONNX SHA256: `01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`
- 정확한 크기: `814591107`바이트 (약 777 MiB).
- 파일 이름은 검증 조건이 아닙니다. 파일 내용의 SHA256과 크기가 모두 맞아야 합니다.
- 이 저장소는 N16의 실제 모델 가중치 파일을 포함하지 않습니다. 당근 개발자의 개인 NAS나 임의 웹 주소를 다운로드하지 않습니다.

## 읽기 전용 상태
젯슨의 최신 보호형 런타임에 코드가 반영된 경우:

```sh
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_offline
```

위 명령은 공식 N16 모델이 연구용 캐시에 있는지만 확인합니다.
모델 파일이 이미 젯슨의 특정 위치에 있다면:

```sh
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_offline --model /실제/보유한/N16.onnx
```

`source_sha_verified=true`는 **파일 동일성**만 의미하며 GPU 실행 성공과 무관합니다.

## 오프라인 연구용 별도 보관

다음 명령은 젯슨 자체에서 실행하며 콤마4와 USB-NCM이 연결되고, 콤마4가
`IsOffroad=1`, `IsOnroad=0`, 외부 AI OFF일 때만 허용됩니다.
P단 단독으로는 승인하지 않습니다. 기존 승인 SSH 키와 호스트 지문 검증은 생략하지 않습니다.

```sh
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_offline \
  --model /실제/보유한/N16.onnx \
  --stage --comma-ssh comma@192.168.100.127
```

작업 중에도 Offroad 상태를 반복 확인하며 중도 실패 시 자기 임시 파일만 제거합니다.
모델을 복사하고도 **2GiB 이상 남는 저장 공간**이 필요하며 기존 연구 파일을 자동 덮어쓰지 않습니다.
저장 위치: `/opt/carrot-jetlink/cache/nexo-n16-offline/carrot-n16-01effc679eb46d60.onnx`.

## 검증 수준 구분

- `source_sha_verified=true`: 사용자가 가진 원본이 당근의 고정 규격과 동일함.
- `model_staged=true`: 해당 ONNX 파일의 격리 캐시 복사가 완료됨.
- `gpu_inference_verified=false`: **실제 N16 GPU/TensorRT 추론 시험은 수행되지 않음**.
- `vehicle_control_enabled=false`: 2019 넥쏘 차량 제어 연결은 하지 않음.
- `hud_prediction_enabled=false`: 외부 HUD 예측 표시도 아직 연결하지 않음.

N16은 기존 NEXO ONNX와 11개 입력/6개 출력 및 recurrent-state 계약이 달라 단순 교체할 수 없습니다.
다음 단계는 실제 Orin Jetson의 OFFROAD 독립 런타임에서
정확한 입력 형식/출력/CPU 기준 수치 검증 후 GPU 엔진 생성과 반복 상태 검증입니다.
주행 제어 전환이나 실제 도로 주행 시험을 의미하지 않습니다.

## 제한 사항

이 작업을 저장소에 반영하고 CI 단위 테스트를 통과하더라도
집에 있는 콤마4/젯슨에 SSH 접속하거나 원본 814MB 모델을 획득했다는 뜻이 아닙니다.
실제 장치 파일 존재·GPU 성능·메모리·온도·설치 결과는 장치에서 따로 확인해야 합니다.
