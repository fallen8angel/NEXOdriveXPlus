# NEXO NCM / HUD 복구 검토 — 2026-10-21

이 변경은 `codex/jetson-ncm-hud-recovery-20261021`에서만 작업한다. NEXO 병합, 장비 접속·설치·재시작, 차량의 약 41개 로컬 수정 변경은 수행하지 않았다. 코드 검증과 실제 장치 연결 성공은 별개다.

## 저장소와 설치 상태의 차이

| 대상 | 확인한 revision / 상태 |
|---|---|
| NEXO 기준 | `7925a461740905bca73216da407c292b5167b069` |
| Carrot carrot-wip | `10cd6868d3715d7068bd38634bf7b808ba9d1834` |
| Carrot carrot-nav | `59eb0f82fa8bf5f2d85106afa446d33197c52df8` |
| carrot-jetson / 고정 서버 | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` |
| 실제 Jetson | 사용자 보고: NEXO install rolled_back, Carrot 서버/HUD active, NCM inactive |

기존 protected-ncm / identity-cast / usb-status-gap 원격 feature branch는 현재 NEXO의 조상이다. NEXO에는 comma NCM 부팅 복구 `2e03c424`, 빈 IPv4 복구 `effba358`, TensorRT 상세 진단 `7925a461`까지 들어 있다. 미병합 PR #27(USB 충돌 진단), #21(YOLO direct), #19(heartbeat 포트)는 별도 유지한다. 이 변경은 그 PR을 일괄 병합하지 않는다.

장비에 선택된 실제 `SOURCE_COMMIT`과 실행 파일 SHA는 아직 전달되지 않았다. 롤백 상태로부터 최신 NEXO가 실행 중이라고 단정할 수 없다. 확장한 읽기 전용 수집기는 current 경로, SOURCE_COMMIT, runtime manifest, 실행 진입점 해시, 서비스 ExecStart/사용자/drop-in, USB/NCM/주소/경로/5599 listener를 수집한다.

## 확인된 원인과 미확정 원인

1. 기존 Carrot Bulk 서버는 `1209:0001`을 기다린다. g1의 `04d8:1234` CDC-NCM과는 전송 방식이 다르다. g1을 해제해서 해결하지 않는다.
2. 보호형 설치는 candidate probe 이후에 NCM 구성을 선택한다. TensorRT 실패로 롤백되면 이전 Carrot release/model과 이전 NCM 구성으로 돌아간다. 따라서 보고된 NCM inactive는 rollback 흐름과 일치한다.
3. comma의 주소 없는 NIC 복구는 NEXO에 이미 수정돼 있다. Jetson `protected_ncm.network_once()`에는 IPv4 조회 `[]`를 NIC 소실로 오인하는 같은 검사가 남아 있었다. 주소와 독립적인 `ip -j link`/ifindex 검사로 수정했다. 이것이 보고된 장비에서 직접 관측된 반환값인지는 미확인이다.
4. 기존 NEXO HUD publisher는 `daemon.serve_modeld()`와 `while enabled()`에 묶인다. Jetlink OFF이면 이 경로에서 HUD도 전송하지 않는다.
5. NEXO HUD의 자체 `hud.lock`만으로는 Carrot boot display와 패널 사용을 직렬화하지 못한다. 고정 서버의 `display_owner.panel_owner(hud=True)`를 각 렌더링 세션에서 재사용한다. snapshot 만료·세션 종료 후 잠금을 반납해 원래 대기 화면이 재개될 수 있다.
6. `wifi_apply.connect()`는 연결된 Wi-Fi도 전달된 프로필도 없을 때 `waiting_for_comma_wifi`를 반환한다. `boot_status.collect()`가 `/dev/shm/carrot-jetlink-network-status.json`을 읽고 `boot_display.lines()`가 Wi-Fi 행에 표시한다. 이는 Wi-Fi provisioning 상태이며 NCM HUD 전송을 막는 조건이 아니다. carrot-nav의 7716 HUD WLAN은 기존 USB에서 별도로 협상하는 경로다. 이를 복사하거나 Wi-Fi 프로필을 바꾸지 않았다.

이전 실기 로그의 `NEXO_ONNX_PREFLIGHT_OK` 다음 `Could not find any implementation for node vision/node_conv2d_1 + vision/node_gelu_1`는 TensorRT 엔진 생성 실패다. 해당 probe에서 HUD import 단계는 앞서 통과했지만, 실제 패널 출력 검증은 아니다.

Conv는 FP16 depthwise(group=64), 입력 `[1,64,64,128]`, weight `[64,1,3,3]`, stride=2, pad=1, 출력 `[1,64,32,64]`; Gelu는 FP16 `approximate=tanh`다. ONNX SHA는 계속 `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`다. UINT8→FP16 입력 사전 검사와 실제 부분 그래프 회귀를 유지한다.

융합 tactic 부재, workspace 또는 CUDA/메모리 문제 중 정확한 근본 원인은 미확정이다. NVIDIA의 [TensorRT 10.3 가이드](https://docs.nvidia.com/deeplearning/tensorrt/archives/tensorrt-1030/pdf/TensorRT-Developer-Guide.pdf)도 workspace와 상세 로그 확인을 안내한다. 숫자 연산, FP16/FP32, tactic, workspace, 모델 SHA를 추측으로 바꾸지 않았다. 기존 `.build.json` 상세 로깅을 재사용하며 동일 설치를 자동 반복하지 않는다.

## 구현과 재사용

- `protected_ncm.py`: 기존 VID/PID+cdc_ncm 발견, 이름 변경 재탐색, NM runtime 관리 제외, DATA 설정/부팅 재생성, lease rollback 재사용. IPv4 `[]` 허용, 독립 link 검사, 변경 전 identity 재검사, `noprefixroute`로 새 /24 경로 대신 peer /32만 추가한다.
- `ncm_display.py`: 명시적 표시 전용 client/server. 기존 v2 TCP와 `ncm_server.tcp_opener`의 USB 전용 bind·peer 제한·재연결을 재사용한다. HELLO capability를 확인하고 HUD/STATE/NAVI만 허용한다. ENGINE/INFER/upload/Wi-Fi 설정/전원 제어 요청은 거부한다.
- 기존 `DisplayPublisher`, snapshot, camera preview, navigation forwarder, HUD renderer/layout, 재연결 supervisor를 재사용한다. 새 차량 계산이나 CAN 전송은 없다.
- `process_config.py`: 별도 표시 worker의 명시적 opt-in 등록. `/data/nexo_jetson_hud_ncm`이 있고 Jetlink enable 파일이 없으며 `NexoJetsonUsb=False`일 때만 실행한다. 기본 OFF다. 기존 네트워크 worker는 추론 OFF에서도 계속 동작한다.
- `native_host.py`: 설치 구성의 `NEXO_JETLINK_DISPLAY_ONLY=1`일 때 GPU engine을 import/load/build하지 않는 host로 분기한다. 기존 inference 모드는 유지한다.
- `protected_install.py`: 새 `--display-only`를 명시적으로 지정한 NCM 설치만 표시 전용 probe를 사용한다. 모델 파일 hash는 계속 검사하되 engine 성공을 주장하지 않는다. 이전 Carrot last-loaded 바이트를 보존하고 `display_only_installed`, `synthetic_inference=false`, `physical_hud_verified=false`로 기록한다. 원래 설치·boot replay·rollback transaction을 재사용한다. 자동 fallback이 아니다.
- `protected_probe.py`: `hud_imports`, `tensorrt_imports`, `onnx_contract`, `engine_build_or_load`, `engine_output_contract` 단계와 오류를 DATA cache의 `nexo-probe.json`에 남긴다. 부모는 이전 report를 지우고 새 실패 단계와 오류를 반환한다. 정상 모델 설치는 기존 실제 engine/출력 probe를 반드시 통과해야 한다.
- `hud.py`: 고정 Carrot panel lock을 공유하고 데이터 소실 시 boot display에 반환한다. 디자인은 수정하지 않는다.
- `server/services/jetson.py`: 표시 전용 NCM 상태를 별도 증거로 읽어 HUD TX/패널 상태를 표시한다. 이를 model ready/active로 표시하지 않는다. 1초 만료를 적용한다.
- `trt_diagnostics.py`: 실제 설치 revision/entry point/service/USB 정보를 추가한다. 수집은 GPU build나 서비스 재시작을 하지 않는다.

## 검증

최종 전체 회귀: **508 passed, 4 skipped, 0 failed**. Windows Python 3.12, 실제 로컬 TCP/UDP, 고정 Carrot 서버, 실제 NEXO ONNX를 사용했다. Linux topology/systemd/GPU/패널은 대역이다.

4 skip: pyray 1, 빌드된 Linux cereal/msgq runtime 2, 실제 Linux flock 1. 기존 pytest/aiohttp 경고 19개. 변경 Python 파일 Ruff, Python 3.10 구문과 diff whitespace 검사 수행.

초기 검사에서 Windows SIGKILL 대역 누락·collector 테스트 allowlist·새 probe 테스트의 환경 변수 복원 누락을 수정했다. Windows 임시 디렉터리 rename WinError 5도 서로 다른 fixture에서 재현되어 **테스트에서만** 실제 rename을 최대 1초 재시도한다. 재시도 후에도 실패하면 테스트는 실패한다. Linux production rename/rollback 코드는 바꾸지 않았다.

검증 범위: g1/ADB 충돌 방지, 양쪽 빈 주소·DOWN·지연 인식·rename·재부팅 상태 재현, 기본 경로 보호, 실제 TCP 재접속, display-only 모델 요청 거부, snapshot TX/RX와 패널 성공 구분, 상태 만료, 기존 ONNX/출력 계약, 설치 실패/rollback/boot replay, manager 등록. 차량 제어·CAN/DBC·MED/SCC·DM·모델·HUD layout 변경 없음.

**실차 미검증**: 실제 단독/동시 재부팅, USB 탈착, 실제 SO_BINDTODEVICE 권한, Linux systemd/파일 권한, TURZX 1cbe:0092 표시·boot screen 인계, TensorRT GPU 엔진 생성/수치/지연, 주행 및 USB 동시 부하. software import probe는 패널 출력 성공 검사가 아니다.

## 집에서 먼저 확인할 읽기 전용 명령

Jetson:

```sh
readlink -f /opt/carrot-jetlink/current
cat /opt/carrot-jetlink/current/SOURCE_COMMIT
cat /opt/carrot-jetlink/updates/nexo-install-status.json
systemctl show carrot-jetlink carrot-jetlink-hud nexo-jetson-ncm -p ExecStart -p ActiveState -p MainPID
lsusb
ip -j link show
ip -j -4 addr show
ip -j -4 route show
ss -ltnp 'sport = :5599'
```

새 패키지의 standalone 수집기를 사용하면 설정 변경 없이 자료를 한 파일로 모을 수 있다(새 결과 디렉터리만 생성):

```sh
sudo python3 trt_diagnostics.py --collect /opt/carrot-jetlink/updates/nexo-observation-$(date +%Y%m%d-%H%M%S)
```

comma(기존 checkout에서 읽기만):

```sh
git status --short
git rev-parse HEAD
ip -j link show dev usb0
ip -j -4 addr show dev usb0
ip -j -4 route show exact 192.168.100.250/32
test -e /data/nexo_jetlink_enabled && echo INFERENCE_ON || echo INFERENCE_OFF
cat /dev/shm/nexo-jetson/ncm-display.json
```

## 실제 적용 전제와 복구

GitHub feature branch만 올려서는 장비에 적용되지 않는다. 적용은 별도 승인을 받은 정차·오프로드 작업이다. 차량의 로컬 41개 수정은 먼저 파일별 diff/백업을 확인하고 선택적으로 반영해야 하며 자동 pull/reset은 제공하지 않는다.

Jetson에는 새 패키지의 installer/helper/runtime이 필요하다. 기존 실패 ZIP의 installer와 새 bundle을 혼합하면 hash 검사에서 거부된다. 승인 후 표시 전용 설치는 기존 `install --bundle ... --sha256 ... --ncm-interface <현재 CDC-NCM 이름> --model-sha256 f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5 --confirmed-offroad`에 **`--display-only`**를 추가한다. 정확한 bundle checksum은 함께 제공하는 manifest/SHA256SUMS를 사용한다. 이 선택으로 Carrot의 기존 서비스 이름을 재사용하며 코드/model/cache는 rollback용으로 보존된다.

comma에는 이 feature 코드와 별도의 `/data/nexo_jetson_hud_ncm` opt-in이 필요하다. Jetlink 추론 파일은 만들지 않고 `NexoJetsonUsb=False`를 유지해야 한다. 표시용 marker 생성도 이번 작업에서는 실행하지 않았다. 집의 offroad 화면 확인은 기존 ClusterHudDebug 설정, 차량에서는 기존 onroad 조건을 사용한다. HUD layout/기능 설정을 임의로 바꾸지 않는다.

설치 실패 시 기존 자동 rollback을 사용한다. 성공 후 명시적 복귀는 새 패키지의 `sudo python3 protected_install.py rollback --confirmed-offroad`로 이전 release/model/NCM 설정을 복구한다. comma에서는 표시용 opt-in만 제거해 중단한다. 예전 설치 ZIP으로 재설치하거나 UDC를 unbind하지 않는다.

최종 현장 확인은 양쪽 ping, peer /32 경로, Carrot 두 서비스+NCM 서비스, 표시 client STATE 수신, HUD TX, 실제 패널 프레임을 각각 확인한다. 그다음 comma만/Jetson만/동시 전원 재인가, 케이블 탈착, Wi-Fi 연결 상태에서도 인터넷 경로가 동일한지 확인한다. 원격 AI 추론은 계속 OFF다.
