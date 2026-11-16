# 2026-11-16 SSH·LTE·GPU 시험·HUD 통합 점검

## 기준과 실제 증거

수정 전 NEXO HEAD는 `77cc03c1b8fc5a3e9382a28a89d0b3f17b640ea3`이다.
공개 HEAD를 다시 조회했다. 당근 openpilot `carrot-wip`는
`90430cd1d3f836a1ea5b64136a61e76c2e6da9ac`, carrot-jetson `main`은
`d0dc96fa32410403dfb2fefff3e2909c11ec109b`이다.
[기존 비교](jetson_carrot_benchmark_20261115.md),
[세션·모델 준비 분석](jetson_integrated_validation_20261115.md),
[후속 비교](jetson_followup_20261115.md)를 검토했다.

제공된 `nexo-jetson-unified-20261116-092723.zip`, `095111.zip`과
IMG_0877~0880을 직접 확인했다. 사진과 진단은 증거로만 취급했다.

| 항목 | 092723 ZIP | 095111 ZIP | 판정 |
|---|---|---|---|
| 차량 | Onroad=1, Offroad=0 | Onroad=0, Offroad=1 | 후자는 실제 Offroad 확인 |
| Comma→Jetson 수집 | 성공 | 성공 | 반대 방향 인증 성공을 보장하지 않음 |
| Jetson→Comma 시험 | 완료 증거 없음 | ssh comma@192.168.100.127 종료255 | stderr 누락으로 인증/호스트키/네트워크 원인 미확정 |
| 독립 GPU 시험 | not_run | not_run | 엔진 빌드/추론 측정 미수행 |
| HUD | 렌더러 실행·패널 전송 보고 | 렌더러 stopped·영상 없음 | 정상 대기와 고장 구분 필요, 물리 화면은 미검증 |
| 패널 간격 | 0.15초 초과4회, 최대0.1795초 | trace 없음 | 실제 검은 화면·기어 누락의 인과관계 미확정 |
| LTE | wlan0 기본 경로·미설정 | 동일, Offroad 변경 조건만 충족 | LTE 부재와 Wi-Fi 우선순위 문제 구분 필요 |

095111에는 현재 SHA `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`
모델의 matching_engine_files가 없다. `09d080...` 엔진은 다른 모델이므로 보존만 한다.
Code10 Conv2D+GELU는 구현/tactic 선택 실패의 증거이지 FP16·메모리 부족 중 하나의
근본 원인을 입증하지 않는다. 기존 기록의 memory_failure_observed와
workspace_warning_observed도 false이며 이는 전체 빌드 중 메모리 부족 부재의 증명은 아니다.
현재 클라우드에서 Comma LTE 주소22번 접속은 connection refused였다. 실제 장치 파일·키·SIM·GPU를
새로 검증한 것은 아니다. 최근 clock_sync 성공/약1초 오차와 과거 시간 오차 이력도 구분한다.

## 실제 변경

1. `ssh_preflight.py`: 고정 NCM 대상에 publickey 전용·BatchMode·엄격 호스트키 검증을
   수행한다. 기존 config/identity/agent/known_hosts의 존재·권한·공개 지문만 관찰한다.
   개인키 내용, 비밀번호, 임의 stderr는 수집하지 않는다. 기존 승인 개인키의 절대 경로를
   선택할 수 있으며 키 생성·복사·교체와 known_hosts 변경은 하지 않는다.
   255만으로 인증 실패를 단정하지 않고 인증 거절/키 권한/형식/호스트키/접속 거절/
   timeout/NCM 이상을 구분한다. 실패 시 CUDA worker를 시작하지 않는다.
   실제 양방향 검증은 독립적이다. 역방향 기록은 boot ID·monotonic freshness를 함께 보관한다.
2. `prepare_engine.py`, `manage.py`, 관리 API/화면: 읽기 전용 역방향 사전검사 버튼과
   기존 키 경로 선택, 구조화된 실패 전달을 연결했다. 시험 전과 실행 중 새 SSH/Offroad
   확인을 계속 수행한다. 로컬·원격 IsOffroad=1/IsOnroad=0 및 외부AI 비활성 조건을 유지한다.
3. `lte_uplink.py`, `lte_share.py`: Wi-Fi가 기본 경로인 경우 무조건 LTE 부재로 처리하던
   제한을 보완했다. Comma의 **기존 PPPSession table1000**과 최신 modem.connected,
   PPP 링크·로컬주소·peer의 일치가 모두 검증될 때만 선택적 정책 공유를 허용한다.
   Wi-Fi 기본 라우트나 modem 설정·DNS를 바꾸지 않는다. Jetson 주소와 NCM 입력 인터페이스로
   한정한 protocol187/priority18700 lookup과18701 unreachable만 관리한다.
   LTE 경로 소실 시 Wi-Fi로 넘어가지 않도록 reject를 먼저 설치하고 마지막에 제거한다.
   local table 우선순위를 확인해 Comma 자체 SSH/HUD 패킷을 보존한다. 외부 규칙 충돌을 거절한다.
   커널 시험에서 확인한 `ip -N`의 숫자 action7(unreachable)도 정확히 인식한다.
   같은 ppp0라도 기본/정책 경로 모드가 바뀌면 이전 인터넷 성공 기록을 무효화하고 명시적 재설정을 요구한다.
   명시적 Offroad enable/disable만 허용하고 읽기 확인·실패 롤백·중간 Onroad 시 복구 보류를 유지한다.
   NAT 설정 상태와 실제 USB에 바인딩한 DNS·인증서 검증 HTTPS 성공은 계속 분리한다.
4. HUD 상태: 실제 Offroad·통신 유지·영상 없음·stopped 보고가 일치할 때 기존 추정 오류를
   WAITING으로 표시한다. 정지 의도나 실제 화면은 검증했다고 표시하지 않는다.
   서비스 실패/blocked/명시적 패널 오류와 Onroad 고장은 숨기지 않는다.
   렌더러·패널 전송·NCM 상태를 별도로 보존한다. 렌더링·기어·차량/H2 장면 코드는 변경하지 않았다.
5. 통합 ZIP: `09_ssh_authentication.json`, `remote/14_ssh_diagnostics.json`에 양방향 근거와
   freshness·실패 안내를 추가한다. LTE 진단에는 선택 경로/PPP 검증 이유를 추가한다.
   새 런타임 필수 모듈2개를 패키지 감사에 포함한다.

## 당근 실제 구현과 적용 판단

| 분야 | 당근 구현/최신 차이 | NEXO 상태와 이번 반영 | 위험/보류 |
|---|---|---|---|
| 모델/캐시 | SHA/TRT/GPU 캐시·가용 메모리40% workspace·timing cache | 이미 pinned builder와 독립 캐시 검증 적용 | 다른모델 엔진 재사용 금지 |
| Conv/GELU | Jetson HEAD 동일, 최신8커밋에 builder 수정 없음 | 기존11종 독립 실험 도구 재사용, SSH 진입 실패 해결 경로 추가 | GPU 없이 정밀도·tactic을 정답으로 단정하지 않음 |
| 입력/출력 | Cinque/NAV와 NEXO 입력상태·출력길이 다름 | f73 원본SHA·I/O 유지 | 당근 모델/JoiningModel 교체 불가 |
| 준비 중 연결 | dcd3392 세션 대기, b128559 느린 응답 후 상태 갱신 | 이전에 이미 적용, 중복 구현 없음 | Onroad 엔진 자동 생성 금지 |
| NAT | Wi-Fi forward의 legacy backend/own-rule/readback | 이미 적용. 이번에는 실제 Comma PPP table 재사용 경로 추가 | Wi-Fi 기본경로/NAT 전체 flush 금지 |
| 원격 업데이트 | 캐시/atomic 저장·복구 | 보호 패키지·원본 보존·검증 실패 복구 이미 적용 | 자동 실차 설치 없음 |
| HUD | 프레임/세션과 수신 증거 | 기존 lease·bounded recovery 유지, Offroad 관찰 보완 | 전송 성공을 물리화면 성공으로 표시 금지 |
| 최신8커밋 | CAN-FD Kick/lever/stop-go·signal 소유권 및 테스트 | 실제 diff 확인, 이번 수정과 무관 | 2019 NEXO 제어/안전으로 복사하지 않음 |

기존 native_fp16/native_strongly_typed를 먼저 개별 실행할 수 있다. fp32_compute,
GELU decomposition/FP32 island, Conv/GELU 부분 그래프와 tactic/workspace 시험은
기존 격리 디렉터리에서만 실행한다. 원본 모델·production 캐시·Display Only는 바꾸지 않는다.
기존 도구는 실제 plan 생성/로드/합성 입력 실행, shape/dtype/유한값과 평균·p50·p95·최대,
동기화 합성FPS·GPU 메모리 표본을 기록한다. 이 값은 카메라 종단 지연·실차FPS가 아니다.
GPU 실측값이나 성공 엔진을 이번 클라우드 작업에서 만들어냈다고 보고하지 않는다.

## 검증 및 현장 최소 절차

CPU 회귀는 원본 SHA·캐시·오류 분류·Offroad 소실·실패 복구·NCM/HUD·기어 누락·업데이트 롤백을
포함한다. 추가 테스트는 SSH 방향/오류분류·비밀정보 제외·키 보존·GPU 시작 차단,
PPP 증거 불충분 거절·정책 충돌·own-rule 정리·Wi-Fi 보존과 Offroad HUD 구분을 검증한다.
실제 커널 NAT/정책 테스트는 CI의 격리된 root network+mount namespace에서만 수행한다.
클라우드 로컬에는 sudo가 없어 이를 로컬에서 실행했다고 보고하지 않는다.
안전/네이티브 모듈 검사를 생략하거나 mock으로 대체하지 않는다. 최종 실행 결과는 커밋 CI와 함께 보고한다.

차량에서 수행할 최소 확인:

1. 기존 보호 업데이트 절차로 새 Comma 코드와 해당 커밋의 Jetson 패키지를 반영한다.
   설치 완료와 실제 HUD·GPU 검증 완료를 구분한다.
2. 관리 화면의 `젯슨 → 콤마 SSH 확인`. 실패코드와 HOME·기존 키/공개 지문/known_hosts를 확인한다.
   별도 승인 키가 이미 있다면 그 경로를 선택한다. 공개키 미등록/개인키 부재/호스트키 변경은
   장치 소유자가 실제 지문을 확인해 기존 SSH 절차로 해결해야 하며 자동 우회하지 않는다.
3. 전원과 접속을 유지할 수 있는 실제 Offroad에서 외부AI OFF를 확인하고
   native_fp16, native_strongly_typed를 하나씩 시험한다. 시동/P단만으로 Offroad를 대체하지 않는다.
   전원이 종료되면 시험을 진행하지 말고 장치 전원 유지 조건부터 확인한다.
4. LTE 상태 확인 후 PPP 검증이 통과할 때만 공유를 직접 켜고 USB 인터넷·DNS 검사로
   DNS·HTTPS 성공을 확인한다. 실패 시 기본 경로를 바꾸지 말고 통합 ZIP을 수집한다.
5. HUD의 P/D/R·수소 표시를 기존 방식으로 확인한다. 새 ZIP에는 SSH/PPP/엔진 단계·측정값이
   포함된다. GPU 합성 성공만으로 AI 주행 또는 JETSON AI ON을 활성화하지 않는다.

실제 Jetson 역방향 SSH 성공, LTE 인터넷, GPU 빌드/추론 성능, 물리 HUD는 현장 미검증이다.

## 변경 파일 전체

- `openpilot/tools/jetson/ssh_preflight.py`, `prepare_engine.py`, `manage.py`: SSH 사전검사와 시험 실행 연결
- `openpilot/tools/jetson/lte_uplink.py`, `lte_share.py`: 기존 PPP 경로 검증·선택 공유·복구·진단
- `openpilot/tools/jetson/runtime_audit.py`: 신규 배포 모듈 필수 검사
- `openpilot/selfdrive/carrot/server/features/jetson.py`, `jetson_pages.py`: API·ZIP·관리 화면
- `openpilot/selfdrive/carrot/server/services/jetson.py`: Offroad HUD 관찰 구분
- `openpilot/tools/jetson/tests/test_ssh_preflight.py`, `test_prepare_engine.py`: 키/Offroad/worker 차단 검증
- `openpilot/tools/jetson/tests/test_lte_uplink.py`, `test_lte_share.py`, `verify_lte_namespace.py`: PPP·정책·복구·실제 커널 패킷 검증
- `openpilot/selfdrive/carrot/server/tests/test_jetson_bundle_api.py`, `test_jetson_local_status.py`: 통합 ZIP/API·HUD 상태 회귀
- `docs/jetson_integration_20261116.md`: 이 조사·변경·현장 검증 보고서
