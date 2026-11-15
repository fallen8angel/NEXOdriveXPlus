# NEXO Jetson·LTE·HUD 후속 수정

## 최신 소스와 범위

시작 NEXO HEAD는 `d12a30b5c89be747910e95e15c1255b9e9b0f5bb`이다.
원격 fetch/HEAD 조회와 GitHub compare API로 다음을 다시 확인했다.

| 저장소 / 브랜치 | 최신 확인 커밋 | 직전 분석 이후 |
|---|---|---|
| NEXOdriveXPlus / NEXO | `d12a30b5c89be747910e95e15c1255b9e9b0f5bb` | 이전 작업 반영 확인 |
| ajouatom/openpilot / carrot-wip | `baab16bb9666eb51a5047aaa586e4517c6155a40` | 추가 커밋 0, compare identical |
| ajouatom/carrot-jetson / main | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` | 추가 커밋 0 |

[기존 벤치마킹](jetson_carrot_benchmark_20261115.md)과 [직전 통합 검증](jetson_integrated_validation_20261115.md)을
검토했다. 기존 SHA/캐시 검사, 이미지 어댑터, Offroad 준비, CUDA cleanup, 완료 추론 증거/만료,
GPU 수집·HUD 추적을 재사용한다. 공개 구현/테스트 존재와 NEXO 차량에서의 실측 성공은 구분한다.
이번 SSH 재시도도 Comma 중계 `172.20.10.11:22`에서 Connection refused였다.
장치 명령·설치·빌드·네트워크 변경은 수행하지 않았다. 실제 GPU/LTE/물리 HUD는 미검증이다.

## 우선순위별 실제 구현 비교

| 순위 / 기능 | 당근 실제 구현 | NEXO 시작 상태 / 차이 | 개선·이번 적용 | 한계·보류 |
|---|---|---|---|---|
| P0 정밀도·엔진 | pinned TRT configure_precision은 FP16 flag를 켜거나 생략 | 원본 그래프가 FP16이므로 flag 생략은 전체 FP32 시험 아님 | 별도 FP32 compute 사본 + FP16/TF32 flag 해제. 원본 바로 다음에 시험 | 산술 변화. 후보 운영 게시·자동 대체 금지 |
| P0 Conv/GELU | 같은 TRT 10.3 빌더, 해당 fused 오류의 직접 수정 없음 | 원본 SHA와 과거 엔진 SHA 불일치. 구현 선택 실패는 확인, 원인 미확정 | 기존 subgraph/FP32 island에 FP32 compute를 더한 7종 실제 build/load/infer·성능·메모리 비교 | GPU 없이 FP16/tactic/workspace/OOM 중 원인 확정 불가 |
| P0 모델 계약 | Cinque/NAV 입력·출력·state feedback | NEXO 고유 history, FP16 `[1,2576]` 출력 | f73… 원본 보존, 값 보존 입력 어댑터 이후 사본 I/O 검증 | 당근 모델/NAV feedback 직접 교체 불가 |
| P1 NAT | wifi_direct_forward.Sharing.ensure의 iptables→iptables-legacy 검사, 소유 규칙·readback | NEXO iptables 고정 사용 | NAT/FORWARD 운용 가능한 backend 선택·저장·동일 backend 복구·설치 후 재검증 | 기존 backend 자동 이동·flush·정책 변경·타 규칙 삭제 없음 |
| P1 콤마4 LTE | 양쪽 tici/modem.py PPPSession이 ppp0·DNS·route 관리 | NEXO 공유 whitelist에 ppp0 누락 | 현재 기본 경로가 ppp0이며 live point-to-point PPP인 경우 지원 | 실차 실제 interface 미확인. Wi-Fi/VPN/임의 PPP 이름 거절 유지 |
| P1 공유 진단 | AP 상태/공유 오류 분리 | 미설정이면 즉시 반환하여 LTE 경로·도구 오류 숨김 | 미설정에서도 로컬 NCM·LTE·방화벽 사전 검사 및 UI/통합 ZIP 표시 | 미설정 조회는 SSH/설정/외부 트래픽 없음. Onroad 변경 금지는 정상 보호 |
| P1 실패 복구 | 소유 NAT만 cleanup, 실패 상태 보존 | 응답 유실 후 부분 적용/정리 실패 시 journal 삭제 가능 | 의도 내구성 기록, 응답 유실 후 readback/원복, 미완료 journal 보존·매 변경 Offroad 검사 | Onroad 전환 시 정리도 보류. 다음 Offroad 수동 복구 필요 |
| P1 인터넷 증거 | AP NAT 및 namespace 테스트 존재 | 직접 USB 인터넷·DNS 검사 없음 | Offroad 수동 버튼으로 SO_BINDTODEVICE+NCM source의 DNS/인증 HTTPS 검사. 시스템 DNS 별도 | Wi-Fi 성공은 USB 성공 아님. 기존 DNS 강제 변경 안 함 |
| P1 준비 중 HUD | dcd3392/b128559 동일 세션 대기·느린 응답 후 heartbeat | 이전 NEXO는 HUD 작업자 생성이 준비 완료 뒤 | 같은 HUD 작업자를 준비 전부터 재사용. STATE 대기/sleep 중 50ms마다 송신 기회 | 실제 transport 오류는 재연결. 150ms 추론 deadline/응답 우선순서 불변 |
| P2 GPU·복구 | preallocation/CUDA graph/동기화·완료 카운터·cleanup | 이미 백포트·유한값/관측 구현 | 기존 코드 재사용, 합성 FPS·메모리 샘플·low-water 기록 | GPU 속도·주행 수치 정확도 미검증 |
| P2 설치·업데이트 | SHA·TRT/GPU별 캐시·job 실패/세션 복구 | protected update·롤백·fsync 존재 | 운영 게시 경로 유지, 실험/로그만 확장 | current/last-loaded/AI enable/모델 join 자동 변경 없음 |
| P2 HUD·기어 | EV6 parked/reverse·renderer | NEXO P/R/D grace·수소/센서 freshness·원래 주행 화면 | 기존 화면 유지, 준비 대기 경로 보완·framebuffer 회귀 유지 | 검은 화면과 gear missing의 인과관계·물리 패널은 현장 확인 |
| P3 객체·신호·주행 | signal_assist/start_guard/stop_prepare는 opt-in 실험·ego-lane 가설 한계 | NEXO observer/shadow·안전 정책 | 기존 신호·안전 회귀 테스트, 제어 복사 없음 | 차량 설정·조향/가감속·DM·안전 정책 변경 보류 |

실제 비교한 소스: [공유 구현](https://github.com/ajouatom/openpilot/blob/baab16bb9666eb51a5047aaa586e4517c6155a40/tools/jetlink/wifi_direct_forward.py),
[공유 테스트](https://github.com/ajouatom/openpilot/blob/baab16bb9666eb51a5047aaa586e4517c6155a40/tools/jetlink/test_wifi_direct_forward.py),
[TRT 빌더](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/third_party/jetlink/jetlink/server/backends/trt/build.py).

## 엔진 검증의 의미

원본 SHA256은 `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`이다.
다른 SHA `09d080f3…`의 기존 plan은 재사용하지 않는다. Code 10은 fused Conv/GELU tactic
선택 실패를 나타내지만 FP16 전체 미지원·workspace 부족·OOM을 단독으로 증명하지 않는다.

새 사본은 FP16 initializer/Constant/내부 타입과 FP16 Cast 목표를 FLOAT로 바꾸고 외부 FP16
입출력은 경계 Cast로 유지한다. ONNX full checker·strict shape inference·동일 I/O를 검사하고,
FP16·TF32 builder flag를 모두 끈다. 산술 변화이므로 numerical_equivalence_verified=false,
vehicle_approved=false이며 운영 캐시에 게시하지 않는다. 유한값/속도 성공은 앞차·plan 정확도 증거가 아니다.

`prepare_engine.py --experiment --samples 100`은 기존 실제 Offroad/Display Only/AI join 비활성,
SHA/ABI/공간/lock/timeout 검사를 유지한다. 7종 순서는 원본 FP16, FP32 compute, FP16 flag 생략,
FP32 GELU island, Conv+GELU/Conv/GELU 추출이다. 실제 build/load/동기화 추론·shape/dtype/유한값,
평균/p95/최대·합성 FPS를 기록한다. 실험 수집 완료는 GPU 성공이나 AI ON이 아니다.
원본 엔진 게시와 실제 외부 AI 전환은 각각 별도 검증이며 후보를 자동 적용하지 않는다.

## LTE 보호와 진단

최초 CI의 실제 kernel 시험은 post-install 경로 재검증에서 실패했다. 기존 코드가 protocol 187을
문자열 `187`로만 비교했으나 iproute2 기본 표시는 `isis`라는 이름이기 때문이다. 경로 조회를
`ip -N -j`로 바꾸고 이전 이름 응답도 동일 번호로 인식한다. gateway/dev/metric 검증은 그대로이며
타 인터페이스의 같은 protocol 경로는 충돌로 거절한다. 재검증 오류에는 실제 규칙/경로/forwarding/
uplink를 담는다. 해당 시험을 삭제·완화하지 않고 재실행한다.

- 공유 기본 OFF, 자동 시작/재연결/주행 중 재작성 없음. 명시적 켜기/끄기/검사와 완전 Offroad 필요.
- 단일 Jetson IP의 NAT/FORWARD만 소유한다. USB gadget/주소, Comma 기본 경로·모뎀 설정,
  Wi-Fi·DNS는 변경하지 않는다. Jetson 경로는 기존 후순위 metric1900/proto187만 사용한다.
- Onroad=1/Offroad=0이면 변경 불가가 정상이다. P단만으로 허용하지 않는다.
- 기존 Wi-Fi 우선순위 유지. 시스템 DNS 실패는 직접 USB DNS/HTTPS 성공과 별도로 표시한다.
  resolver를 덮어쓰지 않으며 변경 필요 시 현장 정책 확인 후 별도 Offroad 작업으로 남긴다.
- 규칙/forwarding/route 존재는 설치 상태다. 실제 Internet 증거는 USB DNS+인증 HTTPS이며
  사용자가 검사할 때만 트래픽을 보낸다. 120초·동일 부팅/설정 조건에서만 결과를 유효하게 표시한다.
- uplink 변경/해제/오류 시 이전 성공을 사용하지 않는다. Offroad 수동 끄기/켜기로 복구한다.

## 변경 파일과 검증 구성

핵심 파일은 `engine_experiments.py`, `trt_diagnostics.py`, `prepare_engine.py`, `lte_share.py`,
새 `lte_probe.py`, `manage.py`, modeld `jetlink/{client,startup,daemon}.py`, 관리 UI
`features/{jetson,jetson_pages}.py`, `jetson_performance.py`, runtime audit이다. 관련 테스트,
`hud-recovery.yaml`과 이 문서, 기존 문서 링크, 앞으로의 개발 절차 `AGENTS.md`도 갱신한다.
controls/monitoring/원본 모델/opendbc/panda는 변경하지 않는다.

CPU 테스트는 실제 ONNX FP32 변환/I/O, pinned builder 호출/flag 제거, 실패·SHA·Offroad·원복,
캐시 미게시, PPP/backend, 명령/SSH 응답 유실·Onroad 전환·journal, DNS 패킷/socket binding/만료,
UI token/confirm/Offroad/update lock, 준비 중 HUD/응답 순서를 검사한다. CUDA/장비 I/O 모사는 실측이 아니다.

CI는 `verify_lte_namespace.py`를 명시적으로 실행한다. 호스트와 다른 net/mount namespace를
확인하기 전에는 변경하지 않는다. 가상 Jetson↔Comma↔WAN veth의 실제 kernel NAT/forwarding이
있어야 ping 왕복이 통과하고, NAT 제거 시 새 목적지 왕복은 실패하며, 해제 후 타 규칙을 보존하는지
검사한다. 장치 신원/Offroad 읽기만 모사한다. 기존 안전성/native extension/HUD 장면/framebuffer/
120초 연속 검사를 유지한다. CI 성공도 실제 LTE/USB/GPU/물리 패널 검증과 구분한다.

## 차량에서 필요한 최소 절차

1. 기존 차량 로컬 수정·설정·모델/캐시를 보존한다. 자동 reset/설치/AI enable은 실행하지 않는다.
2. 수동 업데이트 후 실제 IsOffroad=1/IsOnroad=0, Display Only, AI join 비활성을 확인한다.
3. Jetson에서 기존 `prepare_engine.py --comma-ssh comma@192.168.100.127 --experiment --samples 100`을
   명시적으로 실행하고 통합 ZIP을 수집한다. 원본/FP32·실패 로그·workspace/메모리·평균/p95/최대를
   확인한다. 원본 통과 시 별도 원본 게시를 검증한다. 후보를 실차 모델로 선택하지 않는다.
4. LTE 상태 → 명시적 공유 켜기 → USB 인터넷·DNS 검사. active LTE/backend/rules/route와
   USB DNS/HTTPS·시스템 DNS를 각각 확인한다. 끄기 후 NCM HUD/SSH·Wi-Fi가 유지되는지 확인한다.
5. AI OFF로 P/R/D·수소·짧은 데이터 누락을 확인한다. 합성 성공만으로 AI ON이 되면 안 된다.
   상주 엔진/실제 외부 추론 전환은 이후 별도 Offroad 검증이다.

실제 엔진 생성·GPU 추론·성능 수치·LTE 인터넷·물리 HUD는 현재 미검증이다.
코드/자동 테스트/패키지 게시 완료를 실차 적용 완료로 해석하지 않는다.
