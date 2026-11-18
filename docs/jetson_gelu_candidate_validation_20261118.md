# GELU FP32 기존 엔진의 정확도 검증과 비활성 후보 등록

## 범위와 현재 증거

작업 기준 NEXO: `bf117738ef72bfd076f4fc53cea98c9d353e64e0`.
당근 openpilot carrot-wip: `a9811adce075a81f6a719e611f28ae444193f750`.
carrot-jetson main: `d0dc96fa32410403dfb2fefff3e2909c11ec109b`.
기존 [벤치마킹](jetson_carrot_benchmark_20261115.md)과
[통합 점검](jetson_integration_20261116.md)을 함께 검토했다.

사용자가 확보한 Orin NX 결과는 native_fp16/region_conv_gelu 실패,
region_conv/region_gelu 성공, 전체 gelu_fp32_island 성공(약418.89FPS)이다.
이는 사용자 제공 장치 결과이며 이번 환경에서 재측정한 수치가 아니다.
두 부분 그래프는 실행되지만 결합 경로가 실패한 결과는 fusion/tactic 문제를 좁히는 증거다.
정확한 드라이버/컴파일러 내부 원인이나 수치 동등성까지 입증하지 않는다.

이번 클라우드의 Comma SSH는 접속 거절이었다. 실제 Orin plan·로그·SHA·GPU 성능을
새로 확인하지 못했다. 운영 모델, 엔진 캐시, HUD, 주행 제어는 변경하지 않았다.
이번 요청은 NEXO 대상 검토 PR이며 자동 병합·차량 설치를 수행하지 않는다.

## 기존 기능과 보완한 빈틈

기존 engine_experiments는 이미 단일 trial 선택, plan 생성/로드, CUDA 정리,
shape/dtype/유한값 검사, 동기화 평균·p50·p95·최대와 FPS를 지원한다.
원본 GPU baseline 성공 시 lead/plan 비교도 있다. 그러나 원본 GPU 빌드가 실패하면
전체 모델의 reference가 없어 정확도 미검증으로 남는다. 이 부분을 새로 연결했다.
전체 matrix를 반복하거나 기존 빌더·GELU 변환을 다시 구현하지 않았다.

- `model_accuracy.py`: 원본 ONNX CPU, 후보 ONNX CPU, 기존 후보 plan GPU의 3방향 비교.
- `engine_candidates.py`: 기존 성공 trial의 무결성 확인, 기존 plan 실행, 비활성 후보 영수증 등록.
- `prepare_engine.py`: 기존 flock·독립 worker·Offroad 지속 확인·timeout·설정/릴리스 변경 감시를 재사용.
- `manage.py`, 통합 ZIP API: 검증 단계·실패 원인·수치/성능과 등록 기록의 읽기 전용 수집.
- `runtime_audit.py`, CI: 신규 모듈을 필수 패키지 감사에 포함하고 CPU 기준 실행 의존성 고정.

## 정확도 범위와 입력 계약

원본 SHA는 `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`.
공개 입력의 영상 UINT8와 나머지 FP16, 출력 FP16 `[1,2576]`을 유지한다.
기존 Carrot TensorRT 입력 어댑터는 영상 바인딩만 FP16으로 사용한다. 이 내부 parser 계약과
원본 wire 계약을 구분해 저장한다. 기존 어댑터의256개 byte 값 동치 검사를 재사용하고,
후보 CPU/GPU에도 같은 UINT8 픽셀 값을 FP16으로 전달한다. 다른 dtype/shape 변경은 거절한다.

원본과 기존 parser 후보를 직접 checker/hash 검증하고, 기존 `make_candidate`와 `adapt`로
후보를 **한 번만** 재현해 정확한 변환 SHA가 맞는지 확인한다. 새 plan은 생성하지 않는다.
ONNX Runtime `1.23.2` CPUExecutionProvider를 명시하고 graph optimization을 끈다.
이 기준은 원본 ONNX의 CPU 실행이며, 실패한 원본 TensorRT의 GPU 실행을 대신 검증했다고
표시하지 않는다. Jetson Python3.10/aarch64용 해당 버전 wheel 존재도 확인했다.

8개 결정적 합성 입력은 검정/흰색/무작위 영상과 서로 다른 features/desire/traffic/action을
포함한다. 같은 입력의 해시를 보관하며 이미지는 ZIP에 저장하지 않는다.
전체 출력과 lead/lead_prob/hidden_state/plan 별 최대 절대 오차, RMSE, p95 절대 오차,
허용 범위를 벗어난 원소 수, 최대 허용 오차 비율을 기록한다. 한 케이스의 실패를 평균으로 숨기지 않는다.

고정 screening 정책 `nexo-gelu-observation-v1`은 모든 원소에
`abs(candidate-reference) <= 0.005 + 0.005 * abs(reference)`를 적용한다.
이는 사전에 정한 공학 실험 기준이며 차량 안전 기준이나 수학적 완전 동등성 인증이 아니다.
`numerical_validation_passed`와 별도로 `numerical_equivalence_verified=false`,
`vehicle_approved=false`를 유지한다. 원본CPU↔후보CPU, 원본CPU↔후보GPU,
후보CPU↔후보GPU의 **모든** 케이스가 통과해야 후보 등록을 허용한다.

실제 원본/후보 ONNX를 CPU로 실행한 이번8개 입력의 관측값:

| 원시 출력 slice | 최대 절대 차이 | 최악 케이스 RMSE | screening 범위 초과 원소 |
|---|---:|---:|---:|
| 전체 | 0.125 | 0.0024629343 | 0 |
| lead | 0.00390625 | 0.0003255308 | 0 |
| lead_prob | 0 | 0 | 0 |
| hidden_state | 0.000030517578 | 0.0000019153 | 0 |
| plan | 0.125 | 0.0039728896 | 0 |

원본과 후보는 완전히 동일하지 않다. 혼합 단위의 원시 plan 오차를 실제 거리/가속도 오차로
단정할 수 없다. 이는 합성 CPU 비교 결과이며 Orin GPU 비교·기록 주행 데이터·재귀 상태
오차 누적 검증은 미완료다. 실제 입력 분포/장시간 state feedback/의미별 허용 오차 검토가 필요하다.

## 공간과 보존

기존 `nexo-experiment-*/gelu_fp32_island/experiment.plan`, sidecar, result와
`models/gelu_fp32_island.onnx`만 읽는다. 원본·다른 모델·기존 실험 폴더는 삭제하지 않는다.
검증은 임시 ONNX 약60.8MB와 작은 JSON만 쓰며 임시 ONNX는 검증 후 정리한다.
신규 검증 공간 예산은 원본 파일 크기+256MiB reserve다. 기존 **빌드**의4GiB 최소 조건은 유지한다.
사용자 제공4.13GiB 환경에서도 불필요한 전체 matrix export, plan 복사와 새 엔진 빌드를 피한다.
단, CPU reference 선택 설치에 필요한 공간과 실제 여유 공간은 장치에서 확인해야 한다.

후보 등록은 `cache/engine-candidates/<validation-sha>.json`에 메타데이터만
exclusive/atomic publication+fsync로 저장한다. 기존 영수증을 덮어쓰지 않는다.
공간/기록 실패 시 자기 임시 파일만 정리하고 기존 모델/plan/설정을 보존한다.
원본 모델 SHA 이름으로 이 plan을 production engines 캐시에 넣거나 last-loaded/current를 바꾸지 않는다.

## 장치에서의 명시적 절차

새 코드가 포함된 보호 런타임으로 검토·업데이트한 뒤 실제 Offroad와 외부AI OFF를 확인한다.
기존 승인 SSH 키와 호스트키 검증을 사용한다. 자동 패키지 설치·SSH 신뢰 우회는 없다.
선택적 CPU 기준 의존성은 `onnxruntime==1.23.2`이며 미설치 시 명확히 실패한다.
설치가 필요하면 기존 Offroad 유지보수 절차에서 담당자가 해당 런타임 venv에 추가한다.

성공한 trial 경로는 기존 `cache/nexo-engine-experiments.json`의 stage_directory 또는
통합 ZIP의 engine_experiments 기록에서 확인한다. 다음 경로는 예시이며 실제 기록을 사용한다.

```sh
/opt/carrot-jetlink/venv/bin/python \
  /opt/carrot-jetlink/current/openpilot/tools/jetson/prepare_engine.py \
  --comma-ssh comma@192.168.100.127 \
  --validate-gelu-candidate /opt/carrot-jetlink/cache/nexo-experiment-EXISTING/gelu_fp32_island \
  --samples 20 --detach
```

기존 `--identity-file /기존/승인/개인키`도 지원한다. 검증은 동일 preparation lock을 사용하고
실행 중에도 실제 Offroad·Display Only·current/설정을 감시한다. 상태를 확인할 수 없거나
Onroad로 바뀌면 worker를 종료한다. 새 엔진 빌드나 현재 엔진 전환은 없다.

상태는 `cache/nexo-engine-validation.json`, 상세는 해당 `nexo-validation-*/result.json`이다.
통합 ZIP `remote/15_engine_candidate_validation.json`, `12_engine_candidate_validation.txt`에서
원인별 단계·비교·평균/p50/p95/최대·FPS·메모리·공간 예산을 확인할 수 있다.
성능 시간은 CPU 기준 계산을 제외한 GPU 동기화 실행(입력 host copy 및 host/device 전송 포함)이다.
카메라 종단 latency나 실차FPS는 아니며 사용자 기존418.89FPS와 측정 범위가 다를 수 있다.
읽기 전용 진단은 CPU/GPU 시험이나 후보 등록을 자동 실행하지 않는다.

**감독 프로세스까지 완료한 수치·GPU 검증이 통과한 뒤**, 별도의 명시적 등록:

```sh
/opt/carrot-jetlink/venv/bin/python \
  /opt/carrot-jetlink/current/openpilot/tools/jetson/prepare_engine.py \
  --comma-ssh comma@192.168.100.127 \
  --register-gelu-candidate /opt/carrot-jetlink/cache/nexo-validation-COMPLETE
```

등록 시 동일 부팅·현재 release/원본 SHA·runtime·Offroad·Display Only를 다시 확인하고,
성공한 감독 영수증과 worker 보고서 SHA, 모델/plan/sidecar/trial hash가 모두 맞아야 한다.
정확도 실패·장치 재부팅·증거 변조·부모 감시 실패·설정 변경·다른 작업 lock은 등록을 차단한다.
등록 상태는 `synthetic_validated_observation_candidate`, `active=false`,
`recorded_driving_verified=false`, `vehicle_approved=false`이다.

## 당근 비교와 다음 단계

당근 최신15개 commit 중 TensorRT builder 변경은 없고 carrot-jetson pin도 동일하다.
기존 SHA/TRT/GPU 캐시, 가용 메모리 workspace, CUDA 정리, durable publication은 재사용한다.
당근의 [Cheddar 검토](https://github.com/ajouatom/openpilot/blob/a9811adce075a81f6a719e611f28ae444193f750/docs/cheddar_model_review_20261118.md)는
원본CPU 수치 비교, Offroad 감시, 측정 범위 분리와 모델 교체 보류라는 원칙을 참고할 가치가 있다.
Cheddar는2580출력/명시적 recurrent state 등 계약이 달라 현재 NEXO2576출력에 대체하지 않는다.
최신 constant-Gather 최적화는 QCOM/tinygrad shader 이슈이며 TensorRT GELU 해결책으로 복사하지 않는다.
방향지시등·차량 제어 변경도 이번 범위에 적용하지 않는다.

다음 단계는 실제 Orin GPU3방향 검증 → 기록된 동일 입력/재귀 상태 재생과 의미별 오차 검토 →
명시적 동의 하의 별도 observer 프로세스에서 주행 중 병행 수집/연결 단절·자원 한도 검증이다.
observer는 향후에도 큐/상태/출력을 기존 Comma modeld와 분리하고 제어 출력에 접근하지 않아야 한다.
이번 코드는 observer의 후보/검증 근거 구조만 준비하며 Onroad 프로세스·카메라 경로·제어 연결은 추가하지 않는다.
