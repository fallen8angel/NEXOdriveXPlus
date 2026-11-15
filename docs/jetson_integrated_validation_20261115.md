# NEXO Jetson 엔진·연결·HUD 통합 검증

후속 수정: [FP32 독립 비교·PPP LTE/NAT·준비 중 HUD 검증](jetson_followup_20261115.md).
아래 6종 시험은 d12a30b 시점의 기록이며 후속 코드에는 FP32 compute가 추가됐다.

## 자료와 최신 HEAD

이번 수정의 시작점은 NEXO `4f7c117f2dcdddbbd1fb6f50f6069db4bc85ebb8`이다.
세 저장소의 원격 HEAD를 다시 확인한 결과 다음과 같았다.

| 저장소 / 기본·비교 브랜치 | 확인 HEAD | 제공 기준 이후 커밋 |
|---|---|---|
| fallen8angel/NEXOdriveXPlus / NEXO | `4f7c117f2dcdddbbd1fb6f50f6069db4bc85ebb8` | 0 |
| ajouatom/openpilot / carrot-wip | `baab16bb9666eb51a5047aaa586e4517c6155a40` | 0 |
| ajouatom/carrot-jetson / main | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` | 이전 비교와 동일 |

[기존 비교 문서](jetson_carrot_benchmark_20261115.md)를 검토했다. SHA별 캐시 검사,
원본 UINT8 입력의 값 보존 어댑터, Offroad 격리 준비, 합성 GPU 출력 검사,
완료 카운터 기반 FPS/ON 만료, 비차단 GPU 관측, CUDA 자원 회수, fsync 보강은 이미 구현돼 있다.
이번에는 이를 재개발하지 않고 별도의 비교 실행·준비 연결 유지·데이터 유효성 분리를 추가한다.

요청에 적힌 `nexo-jetson-unified-20261115-124821.zip` 원본은 이번 세션에 첨부되지 않았다.
따라서 최신 상태는 사용자가 제공한 요약으로, 이전에 첨부된 통합 ZIP/TXT는 원본을 읽어
확인한 증거로 구분한다. 최신 파일의 7회 기어 누락을 이 환경에서 직접 재집계했다고 주장하지 않는다.
현재 SSH도 Comma 중계 주소의 22번 포트에서 Connection refused라 장치 명령은 실행하지 못했다.

## 확인한 문제와 확정할 수 없는 원인

- 설치 ONNX `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`와
  기존 엔진의 모델 SHA `09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec`는 다르다.
  이전 엔진은 재사용하거나 삭제하지 않는다. plan 자체 SHA와 원본 ONNX SHA를 같다고 비교하지 않는다.
- `display_only_installed`, 추론 disabled, 현재 모델용 엔진 없음은 서로 다른 상태다.
  Onroad=1/Offroad=0에서는 엔진 생성·설치를 거절하는 것이 정상이다. P단은 충분한 조건이 아니다.
- Code 10과 해당 fused node의 `Could not find any implementation`은 구현 선택 실패까지 증명한다.
  FP16 전체 미지원이나 메모리 부족으로 확정할 근거는 아직 없다. 원본은 FP16 depthwise
  Conv(group 64, 3×3, stride 2)와 tanh GELU이고 ONNX 검사·CPU subgraph 계산은 정상이다.
- 최신 당근 Jetson 빌더는 기존 pinned 빌더와 같으며, 최신 openpilot에도 이 오류를 직접 고치는
  정밀도/tactic 수정은 없다. CUDA 복구 백포트만으로 엔진 빌드 오류가 해결됐다고 할 수 없다.
- 기존 NEXO daemon은 준비 응답을 30초 기다린 뒤 연결을 닫았으며, ENGINE_REQ 자체는
  upstream EngineHost.request를 통해 빌드·모델 교체를 시작할 수 있었다. 준비 대기와
  실제 연결 오류를 구분하고, live 요청을 이미 상주한 정확한 엔진에 대한 bind로 제한했다.
- 기존 P 화면 유지 시간은 이미 3초다. 약 0.1~0.2초 raw gear 누락만으로 검은 화면이
  발생했다고 확정할 수 없다. 다만 raw gear 누락이 최신 수소·TPMS·trip 값까지 숨기는 조건은
  확인했으며, 값 자체의 carState freshness로 분리했다.

## 실제로 빌드·로드·실행하는 독립 실험

새 `engine_experiments.py`는 `prepare_engine.py --experiment`로만 선택한다.
기존 보호 준비 도구의 실제 Offroad 재확인, Display Only/NCM 설정 불변, release/모델 불변,
동일 lock, 4GiB 여유 공간, 제한 시간, worker 종료 처리를 그대로 사용한다.
어떤 결과도 운영 캐시에 게시하거나 last-loaded/current/서비스/Params를 바꾸지 않는다.

| 실험 | 그래프와 빌드 | 판단에 쓸 수 있는 증거 |
|---|---|---|
| native_fp16 | 정확한 원본의 기존 값 보존 입력 어댑터 + 기본 FP16 flag | 현재 모델의 기본 빌드·로드·합성 추론 |
| native_no_fp16_flag | 위와 byte-identical ONNX, builder FP16 flag만 끔 | builder flag 민감도 비교. 네트워크 전체 FP32를 의미하지 않음 |
| gelu_fp32_island | 기존 opt-in 후보 생성기를 재사용, GELU 주변 Cast만 FP32 | precision/fusion 후보 비교. 반올림 변화 가능, 주행 승인 아님 |
| region_conv_gelu | 원본의 실패 구간·가중치를 정확히 추출 | 해당 작은 fused region 자체가 실패하는지 |
| region_conv | 위 Conv만 추출 | Conv 단독 tactic 지원/실행 여부 |
| region_gelu | 위 GELU만 추출 | GELU 단독 tactic 지원/실행 여부 |

전체 모델 3종은 외부 원본 입력/출력 계약을 유지한 원본 또는 사본에서 생성한다.
TensorRT용 입력 어댑터를 통과한 뒤에는 다시 UINT8 어댑터를 적용하지 않고,
정확한 export SHA를 검증해 parser 입력으로 복사한다. 이 변경은 실험 build 동안만 유효하며
실패 시에도 원래 patch 함수를 복원한다. 정상 prepare/install 프로필은 그대로 원본 SHA를 요구한다.
추출 subgraph에는 주행 모델 metadata를 남기지 않는다.

실험은 같은 upstream workspace 정책으로 최초 1회 산정한 고정 예산과 빈 timing cache를 사용한다.
모든 trial은 실제 builder, plan load, warmup, 동기화 추론과 매 출력 shape/dtype/유한값 검사를 거친다.
기본 3 warmup+100 측정이며 평균·p50·p95·최소·최대를 기록한다. 실행 시간은 H2D/D2H와
CUDA stream 동기화를 포함하며 순수 kernel 시간·USB 지연이 아니다.

합성 입력은 이름순으로 고정 seed를 적용해 전체 모델 간 동일하게 만든다. 시간축 재현이나
실주행 정확도 시험이 아니다. 기본 native GPU 시험이 성공한 경우에만 전체 후보 출력의
max absolute error/RMSE 및 원본 lead/plan slice 차이를 비교한다. 기본 native가 실패했다면
후보의 전체 모델 수치 동치는 미검증이다. region은 각 추출 그래프의 ONNX CPU 기준과 비교한다.
출력이 같아도 `numerical_equivalence_verified=false`, `vehicle_approved=false`를 유지한다.

GPU free/total은 CUDA memGetInfo로 전후를 확인하고, build 중 host/unified MemAvailable은
250ms 간격으로 최솟값을 기록한다. host 값은 GPU peak memory가 아니다. 실제 workspace,
ABI/GPU 태그, 단계별 오류, 후보 SHA/plan SHA를 기록한다. SIGKILL도 OOM으로 단정하지 않고
kernel 로그 대조가 필요하다고 표시한다.

각 build는 bounded sidecar와 별도의 VERBOSE 파일을 남긴다. VERBOSE 파일은 각 32MiB까지이며
초과/쓰기 오류를 명시한다. 통합 ZIP의 `remote/13_engine_experiments.json`은 최대 4개 실패의
제한된 발췌를 담고, 전체 build 산출물은 실험 폴더에 보존한다. `experiment_complete`는
비교 수집 종료이지 엔진 성공이 아니다. 각 trial의 `state`와 `native_gpu_verified`를 확인해야 한다.

## 지정 당근 커밋과 기능별 적용 판단

[dcd3392efc0f](https://github.com/ajouatom/openpilot/commit/dcd3392efc0f8d3e426cc7eef912233931a78b46)는
준비 대기가 daemon을 빠져나가 USB owner를 닫던 경로를 바꾸고, 같은 세션에서 STATE를 조회한다.
[b12855935aaa](https://github.com/ajouatom/openpilot/commit/b12855935aaac0c879a942c0c09979f962c98807)는
느린 STATE 응답 후에도 progress를 갱신해 상태 lease가 만료되지 않게 한다. 두 변경은 기준 HEAD에 포함돼 있다.

NEXO는 2초 이내 STATE 전후 heartbeat와 900초 bounded wait를 적용한다. 실제 transport 오류,
실패/잘못된 계약 응답은 계속 오류로 처리한다. 150ms 추론 deadline은 바꾸지 않는다.
새 resident-only capability를 확인한 host에만 bind하며, host는 lock 안에서 정확한 상주
SHA/size/frame_skip을 확인하고 응답한다. EngineHost.request를 호출하지 않아 STATE와 bind 사이에
모델이 내려가도 새 빌드를 시작하지 않는다. 구형 host는 자동 fallback하지 않고 보호 host 필요를 알린다.
일반 live host backend도 build를 거절하며 USB upload 경로도 거절한다. 자동 preload나 다른
control 요청이 우회해 컴파일하지 못하게 하며, 이미 검증된 캐시의 load는 그대로 허용한다.
모델이 아직 상주하지 않았다면 명시적 Offroad 준비·런타임 전환 검증이 남아 있는 것이며,
대기만으로 상주 모델을 바꾸거나 주행 AI를 활성화하지 않는다. Display Only는 계속 ENGINE/INFER를 거절한다.

| 기능 | 당근 구현 | NEXO 현재 | 벤치마킹 가치 | 적용·부작용/위험 | 이번 반영/보류 |
|---|---|---|---|---|---|
| 엔진·모델 | Cinque/NAV 계약·pinned TRT | NEXO 2576 FP16 출력·고유 history | 캐시·검증 절차 | 모델 크기/입출력/slice가 달라 직접 교체 불가 | 원본 유지, 6종 독립 비교 추가 |
| 준비 연결 | dcd3392 동일 세션 대기 | 기존 30초 후 재연결 | 높음 | 그대로 ENGINE_REQ하면 onroad build 가능 | 상주 bind 전용으로 더 엄격하게 적용 |
| 준비 상태 | b128559 응답 후 갱신 | progress heartbeat 존재 | 높음 | 오래된 응답을 AI ON으로 올리면 안 됨 | STATE 전후 갱신·대기 배지·완료 추론 엄격 검증 재사용 |
| USB/NCM | FunctionFS 중심, TCP transport | AGNOS g1·전용 NCM 경로·재시도 | 오류/대기 구분 | gadget·주소·라우트 변경은 정상 연결 훼손 위험 | NCM/LTE 설정 불변, 실제 오류 시 기존 재연결 |
| GPU 최적화 | CUDA graph·preallocation·NAV state feedback | pinned 실행·cleanup 백포트 적용됨 | 일부 높음 | NAV state/projection은 NEXO와 구조 불일치 | 기존 구현 재사용, 실측·고정 조건 비교 추가 |
| 외부 HUD | EV6 parked/reverse 및 모델별 표시 | NEXO P/R/D·수소·센서·차량 모델 | 데이터 유효성/비동기 관측 | EV6 geometry/센서·차량 사진 직접 사용 불가 | NEXO 화면 유지, raw gear와 최신 계기값 유효성 분리 |
| 기어 안정성 | 차량별 gear UI | 두 raw 관측으로 변경·P3s/R0.35s/D1s grace | 누락과 변경 분리 | 임의 gear 생성·오래된 R 영상 유지 위험 | 기존 grace 유지·scene/hold 추적, stale 값 숨김 유지 |
| 신호·주행 안정 | signal_assist/start_guard/stop_prepare는 명시적 opt-in 실험·차로 가설 한계 명시 | 기존 signal observer/shadow·안전 정책 | replay/수치 검증 방식은 유용 | ego-lane 오인·오제동·잘못된 출발 판단 위험 | controller/안전 정책 복사·활성화 보류, 기존 회귀 테스트 유지 |
| 시각/진단 | host 관측과 model session 통계 | 안전한 기존 clock sync와 통합 ZIP | 높음 | wall clock 변경을 처리시간으로 해석하면 오류 | 최근 교정 이력 유지·clock generation, 통신/GPU/실추론 분리 |

## HUD와 시간에 대한 변경

기어 변경 판정·화면 grace 기간은 유지한다. raw gear만 누락되면 P 화면의 수소/TPMS/trip은
각각 최신 carState가 유효한 경우에만 계속 표시한다. carState도 오래되면 기존처럼 숨긴다.
기어값을 만들어 내거나 오래된 데이터를 최신으로 표시하지 않는다. R 카메라 만료·센서 freshness도 유지한다.
기어 missing count/duration, hold start/end, 실제 scene change를 panel gap과 별도 기록한다.
같은 시간대 발생은 인과관계의 증명이 아니며 실제 패널의 검은 화면은 현장 확인 대상이다.

기존 clock sync는 정차 증거·신뢰 가능한 Comma 시각을 확인한 뒤 동작한다. 그 admission은 바꾸지 않는다.
과거 날짜로 부팅한 뒤 시각을 교정하면 이전 로그의 2025년 날짜는 남아 있는 것이 정상이다.
이 현상만으로 RTC 고장/NTP 고장을 확정하지 않는다. 이후 in_sync 조회에서도 마지막 교정의
old drift와 시점을 보존한다. HUD trace는 wall clock step을 별도 generation으로 기록하며
과거 event timestamp를 다시 쓰지 않는다. lease·panel gap·GPU 시간은 monotonic 기준을 유지한다.

공통 UI 배지도 기존 외부 HUD의 strict 완료 추론 증거 함수를 재사용한다. active flag나
갱신된 heartbeat만으로 ON이 되지 않으며 캐시 표시도 실제 완료 시각의 lease에서 만료한다.
상태요청 RTT는 기존 Display Only 측정값을 재사용해 AI OFF에서도 별도 표시한다. 순수 USB 지연은 아니다.

## 검증과 현장 최소 순서

CPU 자동 검사는 원본 SHA/실제 subgraph 가중치·계산, 후보 I/O, 실제 pinned builder 호출과 오류 전파,
GPU 호출을 명시적으로 모사한 load/infer 계약, Offroad 거절·캐시 미게시, parser 복원,
느린 STATE/40초 대기/상주 모델 race/구형 host 거절, 프레임버퍼 P/R/D 및 수소 freshness,
시각 step과 ON 만료를 검사한다. CI는 실제 native 확장 빌드·안전성·NCM·롤백·HUD 장면도 검사한다.
테스트의 fake CUDA는 실제 GPU 성공의 증거가 아니다. 현장 GPU/엔진 성공·평균/p95/최대는 미검증이다.

1. 차량의 로컬 변경을 먼저 보존한다. 자동 reset·설치·AI enable을 실행하지 않는다.
2. 검증된 코드/패키지를 기존 수동 절차로 선택한 뒤, 실제 IsOffroad=1/IsOnroad=0과 Display Only를 확인한다.
3. 필요 시 Jetson에서 기존 venv로 아래 독립 비교 명령을 직접 실행한다. 기존 SSH 인증이 필요하다.

   ```sh
   /opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/current/openpilot/tools/jetson/prepare_engine.py --comma-ssh comma@192.168.100.127 --experiment --samples 100
   ```

4. `cache/nexo-engine-experiments.json`과 통합 ZIP에서 원본 trial, workspace/메모리,
   세분화된 실패와 비교 출력을 확인한다. 원본 GPU trial이 통과했다면 `--experiment`를 뺀
   기존 보호 준비 명령으로 원본 엔진 게시를 별도 검증한다. 후보 성공만으로 원본 엔진을 대체하지 않는다.
5. 합성 시험만 수행한 상태에서 HUD가 AI ON이 되지 않는지, P/R/D 화면·수소 표시·짧은 누락 및
   통신 중단 후 상태 만료를 확인한다. 실제 외부 AI 활성화와 상주 모델 전환은 별도의 Offroad 검증이다.

차량 조향·가속·제동·운전자 모니터링·안전 정책·2019 NEXO 설정·USB/LTE 설정은 수정하지 않았다.
이 문서와 코드 푸시는 실차 설치·GPU 성공·주행 정확도 검증 완료를 의미하지 않는다.
