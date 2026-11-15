# Jetson 엔진 준비와 실시간 추론 진단 (2026-11-15)

이 변경의 원격 코드/자동 테스트 결과와 실제 Jetson 실행 결과를 구분한다.
코드 푸시, 엔진 파일 존재, 합성 추론 성공만으로 차량 AI가 활성화되었다고 표시하지 않는다.
현재 변경은 차량 조향·가속·제동·기존 모델 선택 코드를 수정하지 않는다.

## 확인한 코드 문제와 남은 실기 검증

- 원본 모델 출력은 FP16이다. 기존 보호 probe는 TensorRT 엔진의 **원시 출력**을
  FP32로 고정 검사했다. pinned server는 원시 출력 후 wire 전송 시 FP32로 변환한다.
  probe가 ONNX 선언의 정확한 dtype·shape·유한값을 검사하도록 수정했다.
- ONNX 파일 SHA와 `.plan` SHA는 서로 다른 파일의 해시이므로 같아야 하는 값이 아니다.
  엔진 메타데이터의 `spec.sha256`과 설치 ONNX SHA를 비교한다. 엔진 자체 SHA는
  별도로 기록하며 TensorRT 버전·GPU 태그·실제 loadability와 구분한다.
- UINT8 입력 어댑터와 원본 모델 SHA 검사는 유지한다. 별도 FP32/GELU 실험 모델을
  원본 이름/캐시 키로 위장하거나 자동 채택하지 않는다.
- 기존 TensorRT 10.3 fused Conv/GELU tactic 실패의 하드웨어 원인은 이번 CPU 테스트로
  해결되었다고 주장하지 않는다. 실제 Jetson 빌드 로그와 합성 GPU 시험이 필요하다.
  실패 시 에러·phase·workspace·GPU/TRT 정보·경고 요약을 진단에 남긴다.

## 현장 순서: 설치/설정과 분리된 엔진 준비

1. 정차·시동 OFF 후 콤마4의 **IsOffroad=1 및 IsOnroad=0**을 모두 확인한다.
   P단만으로는 허용하지 않는다. SSH가 기존 USB-NCM 링크를 통해 양방향으로
   동작하고, strict host key 확인과 기존 인증이 설정되어 있어야 한다.
2. 최신 검증된 보호 HUD 번들을 기존 수동 절차로 설치한 뒤, Jetson에서 명령이
   현재 선택된 release에 포함됐는지 확인한다. 이 문서는 원격 설치를 수행하지 않는다.
3. Jetson의 기존 runtime venv로 다음 명령을 **직접** 실행한다. 사용자 이름은
   실제 기존 콤마 SSH 계정을 사용한다. 도구는 키·라우트·NCM 설정을 만들지 않는다.

```sh
/opt/carrot-jetlink/venv/bin/python \
  /opt/carrot-jetlink/current/openpilot/tools/jetson/prepare_engine.py \
  --comma-ssh root@192.168.100.127
```

도구는 콤마의 두 Params를 읽기 전용 SSH로 확인한 뒤, 별도 프로세스와
`cache/nexo-engine-*` 작업 공간에서 현재 ONNX/패키지 계약을 검증한다.
기존 일치 캐시가 있으면 **복사본**을 load/test하고, 없으면 pinned 원본 builder로 생성한다.
빌드 중 Offroad 확인이 끊기거나 실패하면 worker를 종료한다. 준비 제한 시간은 960초다.
기존 보호 runtime ABI(Python 3.10/aarch64/TensorRT 10.3.0)와 DATA 여유 4GiB도 요구한다.
NCM Display Only 설정이 유지되고 콤마 Jetlink 활성화 마커가 없어야 한다. 설정 변경 시에도 중단한다.
현재 release 변경, 모델 변경, 합성 시험 실패 시 기존 엔진을 덮어쓰지 않는다.

합성 영상 입력으로 3회 워밍업 후 20회 출력 shape/dtype/유한값을 검증한다.
평균·p50·p95·최소·최대 시간은 CUDA stream을 동기화하는 `engine.run()` 기준이며,
host/device 전송을 포함한다. 순수 GPU kernel 시간이나 실차 정확도라고 해석하지 않는다.
검증 성공 후에만 원자적 추가로 캐시를 게시하며 다른 내용의 기존 plan/sidecar는 보존한다.
작업 파일과 게시 파일은 inode를 공유하지 않는다. 재시도 시 동일 검증 캐시는 그대로 유지한다.

이 명령은 **Display Only 설정, current, 서비스, last-loaded.json, Params,
`nexo_jetlink_enabled`를 변경하지 않는다.** 따라서 엔진 준비 후에도 기존 HUD 연결은 유지되며
실제 카메라의 지속 추론이나 기존 주행 모델 전환은 별도 현장 검증 대상이다.
엔진 준비를 이유로 `nexo_jetlink_ctl.sh enable*`을 자동 실행해서는 안 된다.
해당 기존 명령은 modeld 전환에 연결되므로 관찰용 GPU 시험과 같은 작업이 아니다.

## 결과와 실시간 표시 확인

- Jetson: `cache/nexo-engine-preparation.json`에서 `state=engine_ready`, 모델 SHA,
  plan SHA, `synthetic_inference=true`, 측정 통계를 확인한다.
  `driving_activation_performed=false`, `vehicle_inference_verified=false`는 유지된다.
- 통합진단 ZIP: `remote/06_model.json`의 engine inventory/build errors,
  `remote/12_ai_inference_readiness.json`의 준비 결과를 확인한다.
  과거 성공 보고서만으로 검증됐다고 하지 않고, 현재 plan 해시까지 일치해야
  `engine_build_verified`와 `synthetic_inference_verified`를 표시한다.
- `04_comma_ai_performance.json/txt`와 HUD: 현재 모델 SHA 일치, 최신 완료 추론,
  프레임 수, 정상 peer 응답 증거가 있어야 ON이다. FPS는 완료 카운터 증가량을
  완료 timestamp 경과시간으로 나눈 관측값이다. `1000/지연시간`으로 계산하지 않는다.
  첫 표본·재시작·만료 시 FPS는 측정 대기이며, 추론 중단 시 ON/FPS를 해제한다.
  합성 probe 수치는 이 실시간 FPS/ON 경로에 전달하지 않는다.

## LTE 공유 진단과 현장 점검

기존 opt-in 공유 설정은 자동 변경하지 않는다. 기존 상태 명령과
통합진단 ZIP의 `08_lte_usb_gateway.json`은 저장 설정 오류, NAT/forward 규칙
조회 실패/누락, IPv4 forwarding, Jetson SSH/기본 경로 실패, 셀룰러 uplink 변경을 구분한다.
규칙이 설치됐다는 사실과 인터넷/DNS가 실제 성공했다는 사실은 다르며 후자는 미검증으로 남긴다.
ZIP에서 LTE 진단이 시간 초과해도 다른 진단 자료는 계속 내려받을 수 있다.

현장에서는 현재 Wi-Fi·USB 경로를 먼저 기록하고, 상태 조회 전후 설정이 같은지 확인한다.
LTE 공유 enable/disable은 별도 명시 작업이며 기존 Offroad 제한을 유지한다.
패키지 다운로드, DNS, 실제 데이터 통신, HUD 화면 유지, 추론 중단·USB 분리 시 상태 만료를
각각 확인한다. 실제 조향·가감속 제어 활성화는 이 점검에 포함하지 않는다.
