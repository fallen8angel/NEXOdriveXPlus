# NEXO TensorRT 10.3 Conv–GELU 실패: 원인 분석과 진단 패키지

이 패키지는 **원인 미확정 상태의 진단 보강판**이다. TensorRT 엔진 생성 오류를 해결했다고
주장하지 않는다. 추가 사용자 지시에 따라 추측에 의한 fusion 차단, FP32 전환, GELU 분해,
workspace 확대, tactic 변경과 자동 재시도를 넣지 않았다.

## 실제 로그와 코드에서 확인한 사실

사용자 장비는 L4T R36.4.7 / TensorRT 10.3.0이며 사용 가능 RAM 약 5.2 GiB,
swap 3.7 GiB다. 기존 ONNX 입력 사전 검사 성공 뒤 아래 fused node에서 엔진 생성이 실패했다.

```text
vision/node_conv2d_1 + vision/node_gelu_1
Could not find any implementation for node
TensorRT returned no engine; see the build log
```

자동 롤백 성공, 기존 Carrot 서비스 2개 active, 기존 모델 정보 유지, 남은 transaction 없음은
사용자가 실제 장비에서 확인한 결과다. 이 작업에서는 장비에 접속하거나 설치하지 않았다.

원본 NEXO 모델은 SHA-256
`f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`,
ONNX opset 20이다. 파일을 변경하지 않았다.

| 항목 | 실제 모델 분석 |
|---|---|
| Conv 입력 | `vision/gelu`, FLOAT16, `[1,64,64,128]` NCHW |
| Conv 가중치 | FLOAT16, `[64,1,3,3]` |
| bias | FLOAT16, `[64]` |
| group / kernel / stride | 64 / 3×3 / 2×2 |
| pads / dilation | `[1,1,1,1]` / `[1,1]` |
| Conv 출력 | `vision/conv2d_1`, FLOAT16, `[1,64,32,64]` |
| GELU | `approximate="tanh"`; shape와 FLOAT16 유지 |
| GELU 출력 | `vision/gelu_1`, FLOAT16, `[1,64,32,64]` |
| 전체 모델 출력 | `outputs`, FLOAT16, `[1,2576]` |

이 연산은 depthwise Conv 뒤 tanh GELU다. NVIDIA 문서는 Conv–GELU의 FP16/INT8 fusion을
지원한다고 설명하고, fused layer 이름은 원래 layer 이름을 결합한다고 설명한다.
따라서 오류는 **해당 fused node의 실행 구현 선택 실패**까지 보여 주지만,
배제된 tactic, workspace 부족, CUDA 오류 중 무엇이 선행했는지는 현재 로그로 구분할 수 없다.
[NVIDIA fusion 문서](https://docs.nvidia.com/deeplearning/tensorrt/10.x.x/performance/optimization.html)

10.3 소스에도 GELU_TANH activation이 있다. FP16/GELU 전체가 미지원이라고 볼 근거는 없다.
`action_t` unused 경고는 이 모델 입력이 사용되지 않거나 compile-time에만 사용된다는 뜻이다.
사용자 로그에서 fatal 오류는 Conv–GELU이며, 이 경고를 원인으로 확정하거나 입력을 삭제하지 않았다.
[TensorRT 10.3 헤더](https://github.com/NVIDIA/TensorRT/blob/release/10.3/include/NvInfer.h)

고정 Carrot 빌더는 weakly typed network, FP16 flag, optimization level 3과 기본 tactic source를
사용한다. workspace는 **MemAvailable의 40%, 최소 256 MiB, 최대 4 GiB**다.
5.2 GiB가 빌드 시점에도 유지된다면 약 2.08 GiB지만 이는 추산이며 실제 사용 값은 새 로그로 확인한다.
swap은 이 계산에 포함되지 않는다. workspace는 tactic 선택의 한도이지 전체 프로세스 메모리 한도가 아니다.
불충분한 workspace는 구현 후보를 제한할 수 있지만 무조건 늘리면 해결된다고 단정하지 않는다.
[NVIDIA 10.3 workspace API](https://github.com/NVIDIA/TensorRT/blob/release/10.3/include/NvInfer.h#L8058)

## 진단 코드

`trt_diagnostics.py`는 고정 Carrot 빌더 SHA와 TensorRT 10.3.0을 확인한다.
그 빌더의 Python 모듈에만 logger를 연결하고 **기존 build_engine을 한 번 호출**한다.
실제 TensorRT 모듈, vendor 파일, ONNX, precision, tactic source, cache key는 변경하지 않는다.
workspace 정책도 그대로 한 번 평가해 upstream 함수에 전달한다.

수집 내용:

- 실제 모델 SHA와 Conv/GELU shape·dtype·속성·전체 입출력 계약.
- TensorRT 버전, 장치 tag, workspace bytes, MemAvailable, FP16 flag, optimization level, timing cache 경로.
- TensorRT VERBOSE/INFO 포함 마지막 메시지 2,000개, 별도의 warning/error 마지막 200개.
- 실패 예외, 시도 횟수 1, 소요 시간, 로그 잘림 여부.
- 기존 WARNING 콘솔 출력은 유지한다.

출력은 `/opt/carrot-jetlink/cache/engines/f73a9e535523d5e9a.*.build.json`이다.
plan/model/일반 engine metadata를 덮어쓰지 않는다. 시작 상태를 먼저 저장하고,
정상 종료/예외 시 마지막 로그를 저장한다. OOM kill/전원 차단 등으로 finally가 실행되지 않으면
시작 상태만 남을 수 있으므로 kernel journal도 수집한다.
진단 파일 저장 오류가 빌드 오류를 가리거나 롤백을 막지 않는다.

기존 `protected_install.py`의 서비스 제어·트랜잭션·자동 롤백은 변경하지 않았다.
USB-NCM, HUD, MED/SCC/레이더/CAN/DBC/차량 제어와 추론 enable 설정도 변경하지 않았다.
Jetlink 차량 추론 기본 OFF를 유지한다. 정상 직렬화라도 runtime probe와 설치 검사를 통과해야
설치 성공이며, `NEXO_ONNX_PREFLIGHT_OK`나 `NEXO_TRT_DIAGNOSTICS_SAVED`는 성공 표시가 아니다.

## 먼저 실행할 읽기 전용 수집

새 ZIP `NEXO_Jetson_protected_NCM_TRT_DIAGNOSTICS_20261021.zip`을 PC에서 해제한다.
옛 실패 ZIP과 이름이 다르며 새 manifest, SHA256SUMS, 내부 코드별 RUNTIME_SHA256.json이 포함된다.
PC에서 ZIP SHA를 제공된 `.sha256`과 비교한다.

```powershell
Get-FileHash .\NEXO_Jetson_protected_NCM_TRT_DIAGNOSTICS_20261021.zip -Algorithm SHA256
```

Jetson에서 새 수신 디렉터리를 만든다. 기존 DATA 아래에서만 수행하며 루트를 rw로 바꾸지 않는다.

```sh
sudo install -d -o jetlink -g "$(id -gn jetlink)" -m 0750 /opt/carrot-jetlink/updates/nexo-trt-diag-incoming
```

PC에서 새 패키지 내용을 전송한다.

```powershell
scp NEXO-protected-ncm-trt-diag.tar.gz NEXO-protected-ncm-trt-diag.json protected_install.py protected_ncm.py trt_diagnostics.py SHA256SUMS RUNTIME_SHA256.json INSTALL_AND_DIAGNOSTICS.md VALIDATION.json jetlink@192.168.45.196:/opt/carrot-jetlink/updates/nexo-trt-diag-incoming/
```

Jetson에서 checksum 검사가 모두 통과해야 다음으로 진행한다.

```sh
cd /opt/carrot-jetlink/updates/nexo-trt-diag-incoming
sha256sum -c SHA256SUMS
sudo /usr/bin/python3 trt_diagnostics.py --collect "$PWD/logs-before-$(date -u +%Y%m%dT%H%M%SZ)"
```

이 명령은 Python 표준 라이브러리만 사용하며 TensorRT/ONNX를 import하지 않는다.
OS 버전, `/proc/meminfo`, 서비스 상태, 관련 service/kernel journal, 설치 상태와 기존 build JSON만 읽는다.
서비스 stop/start, GPU 작업, 설치, 추론 활성화를 하지 않는다.
출력 디렉터리가 이미 있으면 덮어쓰지 않는다. `JETSON_DIAGNOSTICS.json`을 먼저 분석한다.

## 상세 tactic 로그가 여전히 없을 때의 한 번만 수행하는 진단

위 수집만으로 원인이 드러나면 재빌드하지 않는다. 기존 빌드가 WARNING 로그만 남겨
상세 tactic 정보가 없는 경우에 한해, 정차·오프로드에서 **새 진단 패키지로 한 번** 설치 시도를
수행할 수 있다. 이 단계는 GPU 빌드를 수행하고 기존 설치기의 정상 트랜잭션 동안 Carrot 서비스를
중지하므로 단순 읽기 전용 수집과 다르다. 실패 시 기존 롤백으로 돌아간다.
진단 보강판이므로 동일 엔진 오류가 다시 발생할 수 있다. 반복 실행이나 workspace 임의 증가는 하지 않는다.

먼저 기존 서비스 2개가 active이고 transaction이 없는지 확인한다.
`ip -br link`에서 현재 USB NIC 이름을 확인한다. 아래 이름이 바뀌었다면 실제 검증된 이름으로 바꾼다.
installer가 `cdc_ncm` 및 USB 장치 식별자를 검증한다.

```sh
systemctl is-active carrot-jetlink.service carrot-jetlink-hud.service
test ! -e /opt/carrot-jetlink/updates/nexo-install-transaction.json
ip -br link
BUNDLE_SHA=$(/usr/bin/python3 -c 'import json;print(json.load(open("NEXO-protected-ncm-trt-diag.json"))["bundle_sha256"])')
sudo /usr/bin/python3 protected_install.py install \
  --bundle "$PWD/NEXO-protected-ncm-trt-diag.tar.gz" --sha256 "$BUNDLE_SHA" \
  --ncm-interface enx36770f430ea2 \
  --model-sha256 f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5 \
  --confirmed-offroad
```

성공/실패 후 다시 읽기 전용 수집을 한다. 실패 시 서비스 복구 상태와 transaction 유무도 함께 확인한다.

```sh
sudo /usr/bin/python3 trt_diagnostics.py --collect "$PWD/logs-after-$(date -u +%Y%m%dT%H%M%SZ)"
systemctl is-active carrot-jetlink.service carrot-jetlink-hud.service
cat /opt/carrot-jetlink/updates/nexo-install-status.json
test ! -e /opt/carrot-jetlink/updates/nexo-install-transaction.json
```

자동 롤백이 완료됐다면 추가 rollback이나 재설치를 반복하지 않는다.
정상 설치 후 이전 환경으로 돌릴 필요가 있을 때만 기존 명령을 사용한다.

```sh
sudo /usr/bin/python3 /opt/carrot-jetlink/updater/nexo_protected.py rollback --confirmed-offroad
```

transaction이 남거나 rollback incomplete이면 파일/모델/cache를 삭제하지 말고 수집 결과를 보존한다.
이 경우 기존 [보호형 복구 안내](jetson_onnx_identity_cast_20261021.md)의 중단 복구 절차를 따른다.

## 검증 범위

실제 원본 모델 SHA/shape/type 분석, 기존 입력 변환 후 해당 Conv/GELU 그래프·가중치의 동일성,
실제 크기 `[1,64,64,128]` 입력을 이용한 CPU 참조 실행의 FP16 bit 일치를 검사했다.
GPU tactic이나 TensorRT 결과 수치 검증이 아니다.
고정 Carrot 빌더를 실제 호출하되 TensorRT API만 대역으로 바꿔 설정/수치 그래프/타이밍 캐시 유지,
단일 실패 전달, verbose 로그, 디스크 오류, native-thread callback, 읽기 전용 수집을 검증했다.
최종 전체 회귀는 **483 passed, 4 skipped, 0 failed**다. Ruff, Python 3.10 문법,
Git whitespace 검사도 통과했다. skip은 pyray 1개, Linux cereal/msgq 2개, 실제 Linux flock 1개다.
기존 설정/aiohttp 경고 19개가 남아 있다. 새 진단 회귀 14개를 추가했다.
패키지 추출/코드 SHA 검사 결과는 ZIP의 `VALIDATION.json`에 기록한다.

**Jetson GPU 엔진 생성, TensorRT 수치 정확도/지연, 실기 USB/HUD 및 주행은 미검증이다.**
실차 추론을 활성화하지 않는다. 추가 로그로 원인을 확인한 뒤 실제 연산/설정 변경 여부를 결정해야 한다.
