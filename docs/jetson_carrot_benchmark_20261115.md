# 당근 Jetson 비교와 NEXO 관찰 모드 검증 — 2026-11-15

## 조사 기준과 결론

브랜치 이름뿐 아니라 아래 커밋의 실제 소스를 비교했다. 이후 upstream 변경은 이 비교에 포함하지 않는다.

| 저장소 | 확인 브랜치 | 기준 커밋 |
|---|---|---|
| ajouatom/openpilot | 기본 `carrot-wip` | [`baab16bb9666`](https://github.com/ajouatom/openpilot/commit/baab16bb9666eb51a5047aaa586e4517c6155a40) |
| ajouatom/carrot-jetson | 기본 `main` | [`d0dc96fa3241`](https://github.com/ajouatom/carrot-jetson/commit/d0dc96fa32410403dfb2fefff3e2909c11ec109b) |
| fallen8angel/NEXOdriveXPlus | 기본·작업 `NEXO` | 수정 전 `82fbc84e3b18216148eab90363bde195ab92a8d6` |

**공식 carrot-jetson의 최신 빌더는 NEXO가 이미 고정한 버전과 같다.** 최신 openpilot의
빌더 차이는 엔진 파일·메타데이터의 내구성 있는 저장이며, Conv/GELU tactic 선택을
바꾸는 수정이 아니다. 기존 NEXO 모델을 당근 모델로 교체하지 않는다.
공개 소스에 존재하는 기능과 이 차량에서 검증된 기능도 구분한다.

## 실행 구조와 모델 계약

당근의 Comma modeld는 모델 spec을 협상하고 영상 warp·context를 준비해 Jetlink로 보낸다.
Jetson의 EngineHost/세션은 ONNX SHA·TRT 버전·GPU 태그별 캐시를 load/build하고,
TrtEngine이 CUDA 버퍼와 동기화 stream으로 추론한다. 서버의 원시 출력과 wire 전송용
FP32 출력은 서로 다른 단계다. Comma의 JoiningModel이 작은 모델과 큰 모델 결과를 결합한다.
이 경로는 주행 모델에 연결되므로 NEXO 관찰용 도구로 통째로 가져오지 않는다.

근거: [공식 Jetson builder](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/third_party/jetlink/jetlink/server/backends/trt/build.py),
[최신 builder](https://github.com/ajouatom/openpilot/blob/baab16bb9666eb51a5047aaa586e4517c6155a40/third_party/jetlink/jetlink/server/backends/trt/build.py),
[최신 모델 선택](https://github.com/ajouatom/openpilot/blob/baab16bb9666eb51a5047aaa586e4517c6155a40/openpilot/common/jetson_model.py),
[최신 모델 계약](https://github.com/ajouatom/openpilot/blob/baab16bb9666eb51a5047aaa586e4517c6155a40/openpilot/selfdrive/modeld/jetlink/contracts.py).

| 항목 | NEXO 원본 ONNX | 공식 carrot-jetson Cinque v2 | 최신 openpilot 저장소의 NAV RL spec |
|---|---|---|---|
| SHA256 | `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5` | `09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec` | `01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a` |
| 파일 바이트 | 60,792,584 (파일 직접 확인) | 766,040,736 (spec 선언) | 814,591,107 (spec 선언) |
| 영상 입력 | `img`, `big_img`: UINT8 `[1,12,128,256]` | 같은 영상 shape | `new_img [2,6,128,256]`, `state_img_q [2,5,6,128,256]` |
| desire | FP16 `[1,25,8]` | `[1,33,8]` | `desire [8]`, `state_desire_q [132,1,8]` |
| feature | FP16 `[1,24,512]` | `[1,32,32,512]` | `state_feat_q [128,1,16384]` 등 명시적 state |
| 기타 | FP16 `traffic_convention`, `action_t`: 각각 `[1,2]` | 두 입력 `[1,2]` | 두 입력 + `nav_map [1,1,256,256]`, `navigation [1,47]` |
| 출력 | FP16 `outputs [1,2576]` | `outputs [1,18452]` | `outputs [1,18580]` + `next_state_*` |
| hidden state 크기 | 512 | 16384 | 16384 및 추가 map/navigation state |

Cinque/NAV의 표는 JSON spec 비교이며 해당 ONNX 가중치 전체를 다운로드·추론해 검증한 결과가 아니다.
최신 openpilot은 TICI의 모델 카탈로그 선택도 사용한다. 저장소 NAV spec이 모든 차량의 실제
선택 모델이라는 의미는 아니다. historical Cinque spec도 유지된다.

NEXO의 출력 slice는 `lead [917:1061]`, `lead_prob [1061:1064]`,
`hidden_state [1064:1576]`, `plan [1576:2566]`이다. 당근의 더 큰 출력 tensor를
이 오프셋으로 해석할 수 없다. 기존 앞차 예측·주행 계획 차이가 발견된 FP32 실험 모델은
I/O shape만 같다고 승인하지 않으며, 원본 모델·정밀도·파서·차량 설정을 보존한다.

## 기능별 비교와 적용 판단

| 기능 | 당근 소스 | NEXO 기존 구현 | 이번 판단 |
|---|---|---|---|
| SHA/TRT/GPU별 엔진 캐시 | EngineCache | 동일 pinned backend + 독립 plan SHA/metadata 검사 | 이미 구현, 재개발 안 함 |
| 원본 모델 입력 어댑터 | UINT8 head 패치 | NEXO 정확한 SHA, 256개 byte 입력 동치 검사 | 이미 구현, 원본 계약 유지 |
| 합성 GPU benchmark | stream 동기화 engine.run | shape/dtype/유한값 검사, 평균·p50·p95·최대 | 독립 준비는 기본 100회(2~100 지정), 설치 probe는 기존 20회. 실제 장치 결과는 미검증 |
| Offroad 준비·실패 복구 | resident 준비 대기/세션 복구, 계약 위반 시 실패 | Offroad 재확인, 격리 worker, timeout, 설정 불변·캐시 충돌 거절 | 이미 구현, 자동 주행 전환 추가 안 함 |
| 저장 내구성 | 최신 builder/cache: 파일·디렉터리 fsync + atomic replace | 격리 복사·no-clobber link, fsync 누락 | 수정: 공개 전 두 파일 fsync, 공개 후 디렉터리 fsync, 실패 시 자기 파일만 회수 |
| 부분 CUDA 초기화 복구 | 최신 engine ExitStack/graph cleanup | pinned engine + 실패 worker 종료로 프로세스 격리 | 독립 Offroad 준비·보호 AI probe에 cleanup만 백포트. 일반 주행 backend 유지 |
| Conv/GELU 실패 기록 | 기본 parser/build 오류 | VERBOSE bounded tail·실제 sizing·ABI 기록 | 새 적용: 증거별 분류·권고 검사·원인 미확정 표시, 재시도/정밀도 변경 없음 |
| 과거 실험 파일 | NEXO 고유 경로 없음 | 정식 캐시 중심 수집 | 새 적용: 지정 실험 파일 SHA·로그, 임시 build sidecar를 읽기 전용 수집 |
| 실제 추론 ON/FPS | 완료 추론/세션 통계 | strict SHA·완료 수·양측 timestamp, 외부 HUD badge | 이미 구현; synthetic/엔진 존재로 ON 금지 유지 |
| GPU·AI 관리 화면 | 별도 host_performance observer | 5초 cached history + 통합 ZIP, 관리 화면 상세 부족 | 새 적용: 비차단 read-only API와 화면, ON/FPS·왕복 p95/max·GPU 부하/클럭, 독립 만료 |
| HUD 차량·기어·계기 | upstream HUD | NEXO 외부 HUD·기어별 표시·재연결 | 기존 구현 유지, 프레임 렌더·전송 경로 변경 안 함 |
| USB/LTE | bulk FunctionFS, TCP transport 지원 | AGNOS g1 USB-NCM, TCP_NODELAY/keepalive/deadline, LTE 공유 진단 | 이미 구현, USB gadget·IP·라우트·NAT·버퍼 임의 변경 안 함 |
| NAV RL stateful GPU feedback·projection | 모델 전용 state·출력 선택 최적화 | NEXO queued feature 계약 | 모델 구조 차이로 직접 적용 불가 |
| 최신 JoiningModel 결합·작은 모델 overlap | 전용 입출력/결합 경로 | NEXO 기존 주행 모델 | 제어 경로 변경이므로 이번 적용 제외 |

후속 비교에서도 upstream 두 HEAD는 위 기준 커밋과 같았다. 이번 추가 반영은 이미 가져온 fsync나
상태 UI를 중복 구현하지 않고, 기존 `TrtEngine.run/_enqueue`를 상속하는 별도 관찰용 클래스의
자원 소유권만 보강했다. 원본 엔진 파일 SHA와 TensorRT 10.3.0을 먼저 확인하며,
부분 할당 실패·capture begin/enqueue/end/instantiate 실패·해제 실패를 CPU fault injection으로 검사한다.
해제 실패 시 나머지 자원 회수를 계속하고, GPU 검증 결과를 성공으로 게시하지 않는다.

첨부 현장 진단에서 나타난 과거 build sidecar의 `failure_diagnosis=null`도 읽기 전용으로 보완한다.
현재 분류 함수를 SSH helper에 함께 전달해, 예전 설치본과 과거 로그도 같은 기준으로 해석한다.
sidecar를 고쳐 쓰거나 모델을 재빌드하지 않는다. SSH helper는 기존 runtime venv Python을 우선하고,
없으면 시스템 Python으로 돌아간다. Pillow를 설치하거나 서비스 환경을 변경하지 않는다.
8초 진단의 추가 Jetlink 검사는 기존 5초 제한을 유지하면서 unbuffered 출력과 timeout 당시의
부분 관측값을 보존한다. 수집 timeout과 GPU 추론 실패는 구분한다.

## Error Code 10 분석과 변경 범위

제공된 로그의 `Could not find any implementation`은
`vision/node_conv2d_1 + vision/node_gelu_1` fused node에 사용할 구현을 선택하지 못했다는
증거다. ONNX full checker와 shape inference로 원본의 FP16 depthwise Conv
`group=64`, 가중치 `[64,1,3,3]`, stride 2, pad 1과 tanh GELU를 확인했다.
입력 `[1,64,64,128]`, 출력 `[1,64,32,64]`다.

**코드 10만으로 FP16/GELU 미지원, workspace 부족 또는 CUDA OOM이라고 확정할 수 없다.**
이번 CPU 테스트에서 원본과 정식 입력 어댑터의 해당 subgraph 바이트 및 실제 가중치 계산
결과가 동일했다. GPU tactic 선택과 전체 모델의 수치 정확도를 증명한 것은 아니다.

공식 pinned builder SHA는 `35e9a5f57b1a875ce5d8acac2a3e774051351a38fcd3738f99663628a3c6bba8`다.
최신 openpilot과 비교해 FP16 flag, optimization level 3, workspace 계산, tactic source에
관련 해결 변경이 없었다. 자동 FP32 fallback/GELU 분해/tactic 차단/메모리 확대를 넣지 않았다.
이전 `09d080f36965bb2a.trt10.3.0.Orin-sm87.plan`은 다른 모델용이므로 재사용하지 않는다.
ONNX SHA와 plan SHA는 서로 달라도 정상이며, 엔진의 `spec.sha256`이 ONNX SHA와 일치해야 한다.

진단은 parser 실패, Conv/GELU tactic 미선택, 일반 tactic 미선택, 메모리 할당 실패,
미분류를 구분한다. workspace 경고와 실제 메모리 오류도 분리한다.
실패 sidecar와 준비 결과에 `failure_diagnosis`를 보존하고, 예전 로그도 수집 시 분석한다.
실제 배제된 tactic과 실패 전후 GPU 메모리는 현장 VERBOSE 로그로 확인해야 한다.

## 통신·지연 진단

upstream bulk USB와 이 차량에서 정상 작동 중인 NCM은 다른 gadget 구성이다.
검증된 NCM 연결을 bulk로 바꾸지 않는다. TCP_NODELAY/keepalive·deadline·비차단 처리는
이미 있다. upstream의 4MiB TCP buffer를 그대로 설정하면 메모리·대기열 지연이 바뀔 수 있어
실측 없이 적용하지 않는다. NCM 자체가 오류 원인 또는 고지연 원인이라는 증거도 없다.

현재 pinned engine 실행 시간에는 host/device 복사와 stream 동기화가 포함된다.
`modeld_roundtrip_ms`는 준비·대기·전송·실행을 포함하는 왕복값이다. 이 두 값의 차이를
순수 USB 지연으로 표시하지 않는다. 서로 다른 장치의 monotonic 시계를 직접 빼지 않는다.
독립 엔진 benchmark는 평균·p95·최대를, 실제 modeld rolling window는 왕복 p95·최대와
표본 수를 제공한다. 실제 FPS는 완료 수 증가량으로 계산한다.

새 `/api/jetson/ai-performance`는 기존 5초 GPU 기록만 SSH로 읽는다. 원격 조회는
6초 이상 간격·동시 1개·기존 SSH timeout으로 제한한다. HTTP·modeld·HUD는 조회를 기다리지 않는다.
엔진 파일을 반복 해시하거나 서비스 제어·설정 변경을 하지 않는다. GPU 기록의 Jetson-local age에
SSH 시간과 수신 후 경과를 더해 만료를 판정한다. AI ON은 마지막 완료 timestamp의 남은 lease에
따라 브라우저에서도 만료한다. 통신 지연이나 화면 새로고침이 오래된 증거를 갱신하지 못한다.

## 검증 상태

| 항목 | 결과 |
|---|---|
| 원본 ONNX SHA·full checker·계약·실제 Conv/GELU CPU 계산 | 자동 테스트로 확인 |
| no-clobber 저장·파일/디렉터리 fsync 실패 복구 | fault injection 자동 테스트 |
| 엔진만 준비·GPU busy·오래된 기록의 AI ON 거절 | 자동 테스트 |
| 느린 SSH·실패·타깃 변경·종료 시 task 취소 | 자동 테스트 |
| 실제 페이지 JS 문법·버튼·ON 만료 | Node 실행 자동 테스트 |
| 모델·controls·monitoring·opendbc·panda 차이 | 수정 전 HEAD 대비 변경 없음 |
| 실제 SSH | Comma `172.20.10.11:22` Connection refused; Jetson 명령 실행 안 됨 |
| 실제 설치 모델·실험 patched 파일·plan SHA | 미검증: 장치 파일을 읽지 못함 |
| 실제 TensorRT 10.3 빌드·GPU 출력·평균/p95/최대 | **미검증 / 실측값 없음** |
| 기존 실험 2.93ms | 사용자 제공 과거 값; 이번 모델/엔진의 검증값으로 재사용 안 함 |
| 실제 화면 깜박임·기어 전환·장시간 NCM·LTE·주행 정확도 | 현장 미검증 |

이 환경의 source-only Python에는 `params_pyx`가 없어 전체 runtime import 두 건이 실패했다.
mock으로 대체하거나 안전성 검사를 제거하지 않는다. GitHub의 native build 후 전체 테스트에서
실제 모듈 import와 안전성 회귀를 함께 검증한다. 실제 GPU 성공은 CPU/CI 성공으로 대체하지 않는다.

## 현장 확인: 최소 순서

1. 정차·시동 OFF에서 콤마 `IsOffroad=1`, `IsOnroad=0` 및 기존 Display Only를 확인한다.
   새 코드를 자동 설치하거나 AI enable 명령을 실행하지 않는다.
2. 기존 수동 절차로 검증된 패키지를 선택한 뒤, Jetson runtime venv에서 아래 **독립 준비 명령**을
   직접 실행한다. `comma`는 사용자가 제공한 계정이며 USB peer 주소는 기존 보호 도구가 요구하는
   `192.168.100.127`이다. 이 경로의 실제 SSH 성공 여부는 별도 미검증이다. 실패 시 멈추고
   기존 키·host key·주소를 점검하며 IP/라우트를 임의 변경하지 않는다.

   ```sh
   /opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/current/openpilot/tools/jetson/prepare_engine.py --comma-ssh comma@192.168.100.127
   ```

3. `cache/nexo-engine-preparation.json`에서 `state=engine_ready`, 현재 모델 SHA,
   `synthetic_inference=true`, `synchronized_inference_ms`의 평균·p95·최대를 확인한다.
   실패하면 기존 엔진·설정이 그대로인지 확인하고 통합진단 ZIP을 저장한다.
   필요 시 기존 진단 수집기를 `--collect`에 새 출력 디렉터리를 지정해 실행하면
   실험 파일 SHA와 알려진 위치의 build 로그도 저장한다. 원본/실험 수치 동치와 실차 승인은 별도다.
4. `/jetson`의 AI·GPU 실측과 외부 HUD를 확인한다. 독립 합성 시험 후에는 AI ON이 뜨지 않아야 한다.
   P/R/D 화면·차량 계기·HUD 통신을 확인한다. 실제 추론이 없는 상태는 대기여야 한다.
   향후 별도 관찰 테스트에서 완료 추론이 확인된 경우에만 ON/FPS가 나오고, 중단 시 만료되어야 한다.

이 절차는 모델 전환, 조향·가속·제동 연결을 포함하지 않는다. TensorRT 오류 해결 완료 선언은
현재 SHA에 대한 실제 Jetson build·load·합성 GPU 검증 로그를 확보한 뒤에만 가능하다.
