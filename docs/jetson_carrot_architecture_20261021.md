# Carrot 연결 구조 비교와 NEXO 후속 복구 — 2026-10-21

작업 브랜치: `codex/jetson-carrot-recovery-20261021`. 기반은 [PR #30](https://github.com/fallen8angel/NEXOdriveXPlus/pull/30)의 `0c29d2ac9290451eb70c6f4a317964b31d7db7d0`이다. 기존 NCM/HUD 복구, 보호형 설치, ONNX 수정과 배포 ZIP을 보존했다. NEXO에 병합하지 않았으며 장비 접속·설치·pull·USB/Wi-Fi 설정 변경도 실행하지 않았다.

## 기준과 실제 설치 상태

GitHub API의 HEAD와 로컬 Git object를 함께 확인했다. 아래 SHA에 고정한 소스 비교이며 실차 벤치마크 수치는 아니다.

| 저장소/브랜치 | 확인 HEAD |
|---|---|
| NEXOdriveXPlus/NEXO | `7925a461740905bca73216da407c292b5167b069` |
| ajouatom/openpilot carrot-wip | `10cd6868d3715d7068bd38634bf7b808ba9d1834` |
| ajouatom/openpilot carrot-nav | `59eb0f82fa8bf5f2d85106afa446d33197c52df8` |
| ajouatom/carrot-jetson main / NEXO 고정 서버 | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` |

사용자 보고상 Jetson은 설치 실패 후 `rolled_back`, 기존 Carrot 서버/HUD active, NCM service inactive다. **실제 설치 SHA와 실행 파일 해시는 미확인**이다. 팬·시작 화면·active 상태는 NEXO TCP 서버, 정상 TensorRT 엔진, HUD 차량 데이터 수신의 증거가 아니다. 실제 `current/SOURCE_COMMIT`, `NEXO_RUNTIME.json`, 서비스 `ExecStart`, entrypoint SHA를 읽기 전용 수집기로 대조해야 한다.

기존 NEXO에는 comma IPv4 없는 상태 복구와 추론 OFF에서도 동작하는 네트워크 worker가 있다. PR #30에는 Jetson 주소 없는 상태 수정, 표시 전용 NCM 세션, probe 단계 분리, boot display 패널 잠금 공유가 있다. 이 기능들을 다시 구현하지 않았다.

## 실제 함수별 비교

아래 Carrot 경로는 `ajouatom/openpilot` 기준이다. `third_party` 경로도 같은 SHA의 트리에서 확인했다.

| 과정 | Carrot 실제 소스/함수 | NEXO 적용 판단 |
|---|---|---|
| 1. comma USB 생성 | `tools/jetlink/setup_gadget.sh`, `openpilot/selfdrive/modeld/jetlink/mobile.py:setup_gadget()`, `daemon.py:main()`, `FfsTransport` | `/sys/kernel/config/usb_gadget/jetlink`, VID/PID `1209:0001`, FunctionFS descriptors 후 bind. 다른 gadget이 UDC를 점유하면 거부한다. **g1 공존을 해결해 주는 코드가 아니다.** NEXO의 기존 g1/NCM 사용 유지. |
| 2. USB 인식·협상 | `third_party/jetlink/jetlink/transport/usbbulk.py:UsbBulkTransport.open()`, `server/main.py:_usb_opener()`, comma `daemon.py:greet()`, `compat.py:ProtocolChoice` | libusb VID/PID·endpoint 검사와 HELLO, 프로토콜 선택. NEXO는 검증된 g1 인터페이스·USB peer 경로에 고정한 TCP/v2 HELLO와 NEXO capability 사용. |
| 3. 전원 ON | `install_server.sh`의 multi-user service/Restart=always/3초, `server/main.py:_serve()`; comma manager가 `daemon.main()` 실행 | NEXO 기존 manager 네트워크 worker, protected_ncm 부팅 재생성, 기존 서비스 이름 재사용. 프로그램 설치·opt-in 없이는 자동 연결할 수 없다. |
| 4. 연결 실패 재시도 | `_usb_opener()` 실패→None, `_serve()` 2초 대기, `daemon.main()` cleanup 후 2초 재시도 | NEXO `ncm_server.tcp_opener()`가 주소·경로 준비 전 None/로그; `ncm_display.main()` cleanup 후 2초 재접속. 이번에 HUD 수신 확인 불일치도 이 경로로 보낸다. |
| 5. USB 탈착 | `daemon.host_attached()`, `close_resources()`, transport close, 새 HELLO; `MobileCable._check_interface()`의 ifindex 검사 | NEXO `ncm_transport.host_interface()` 재탐색, listener ifindex 변경 시 재생성, 새 TCP session/snapshot generation 사용. 이번에 무응답/미완성 표시 세션도 3/5초 내 해제. |
| 6. 동시 재부팅 | 위 서비스 재시작과 반복 탐색의 조합, 정해진 부팅 순서를 요구하지 않음 | 소스상 자동 대기/재시도 준비. **실제 양쪽 동시 재부팅은 미검증**. |
| 7. 전용 AP | carrot-nav `wifi_direct.py:configuration(), choose_subnet(), channel(), AccessPoint.start(), ensure(), serve()`, `wifi_direct_netlink.py:concurrent_station_ap(), Wireless.create()` | 동시 STA/AP capability 확인 후 wlan0와 같은 PHY에 `wljetson0` 생성, 별도 프로필·SSID·키, 충돌 없는 subnet, 기존 채널을 고려. wip와 고정 Jetson main에는 이 AP 파일이 없다. NEXO는 설계 단계. |
| 8. HUD 차량 데이터 | `hud_protocol.Publisher`, `hud_wifi.py:publisher(), WifiPublisher`, `hud_wifi_worker.py:Runtime.sender()` | snapshot/media를 저우선순위 worker로 분리. nav의 Jetson 경로는 7716 WLAN으로 전송; NEXO는 기존 snapshot/preview/nav producer와 NCM 표시 경로 유지. |
| 9. TURZX 출력 | `hud_wifi_worker.Runtime.receiver()`, `hud_navi.HostForwarder`, `hud.py:RemoteSubMaster, main(), wait_for_usb_display(), require_usb_display()`, `display_owner.panel_owner()` | Jetson 수신 packet→기존 renderer→TURZX USB-A. NEXO `tools/jetson/hud.py`의 adapters, 메뉴/주차/카메라/디자인과 PR #30 패널 잠금 재사용. |
| 10. 추론/HUD 분리 | `hud_wifi.WifiPublisher.packet()/media_packet()`는 None, 별도 worker가 WLAN send/ACK. 제어 FD와 USB 세션 키 수명에 종속 | 프레임별 USB HUD 쓰기·WLAN 대기를 추론 owner에서 제거하는 장점. **초기 협상과 worker 생명주기는 USB 세션에 의존**하고 CPU/GPU/radio 자원은 공유한다. 완전 독립 또는 무부하라고 볼 수 없다. |

주요 원문:

- [nav gadget](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/setup_gadget.sh), [nav daemon](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/openpilot/selfdrive/modeld/jetlink/daemon.py), [wip mobile](https://github.com/ajouatom/openpilot/blob/10cd6868d3715d7068bd38634bf7b808ba9d1834/openpilot/selfdrive/modeld/jetlink/mobile.py)
- [nav AP](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/wifi_direct.py), [STA/AP capability](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/wifi_direct_netlink.py), [Wi-Fi provisioning](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/wifi_publisher.py)
- [HUD negotiation](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/hud_wifi.py), [HUD sender/receiver](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/hud_wifi_worker.py), [server capability gate](https://github.com/ajouatom/openpilot/blob/59eb0f82fa8bf5f2d85106afa446d33197c52df8/tools/jetlink/server.py)
- [고정 Jetson Wi-Fi worker](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/tools/jetlink/wifi_apply.py), [고정 boot display](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/tools/jetlink/boot_display.py)

### Bulk, Carrot mobile NCM, NEXO NCM은 서로 다른 구성

Carrot wip/nav의 mobile auto는 자체 `jetlink` composite gadget에 NCM을 추가한다. comma가 `192.168.60.1:5599` listener/DHCP를 제공하고, TCP grace와 제한된 USB probe를 순차 실행한다. probe가 gadget을 재열거할 수 있다. Jetson 경로는 `mac.prepare()`에서 USB mode와 고정 `JETSON_SPEC`을 요구하고 signed updater가 모델을 준비한다. 이 mobile NCM 코드를 NEXO Jetson 서버에 그대로 적용할 수 없다.

NEXO는 **이미 존재하는 g1 `04d8:1234` CDC-NCM**에 comma `.127`, Jetson `.250`, Jetson listener `192.168.100.250:5599`로 연결한다. `network.ensure_ncm_network()`, `protected_ncm.discover()/network_once()/network_loop()`, `ncm_transport.require_usb_route()`가 VID/PID·cdc_ncm·ifindex·주소·peer /32 경로를 확인한다. 기본 경로/Wi-Fi 대신 USB peer 경로만 관리한다. 이 구조를 유지했다.

### 전용 Wi-Fi의 조건과 복구 한계

`wifi_publisher.collect()`가 root worker에서 저장된 Wi-Fi 프로필과 전용 AP를 읽고, `wifi_protocol.Publisher.send()`가 `carrot_wifi_v1`/0x4002 private USB 메시지를 보낸다. 프로필을 Params/HUD/로그에 넣지 않는다. `wifi_apply.Worker.step()`는 Jetson의 별도 root service에서 저장 프로필 재연결/실패 시 복원을 수행한다. USB/모델/업데이트 성공과 분리된 서비스다.

`wifi_direct.serve()`는 AP 시작·재생성을 offroad에서만 시도하고 5초 점검, 실패 후 30초 대기한다. 이미 살아 있는 AP는 onroad에도 유지하지만, 주행 중 AP 소실 시 즉시 새 AP를 만들지는 않는다. branch source identity와 소유 ifindex를 확인해 자기 자원만 회수한다. `wifi_publisher.direct_client_id()`는 boot ID/AP ifindex 변경을 반영해 Jetson의 연결 재시도를 유도한다.

HUD는 별도로 `carrot_hud_wifi_v1`, 0x4020/0x4021로 32-byte 세션 키를 협상한다. `server.CarrotSession`은 실제 `UsbBulkTransport`일 때만 이 capability를 제공한다. `hud_wifi.publisher()`는 Jetson peer가 이를 지원하지 않거나 협상에 실패하면 None이며 **USB HUD로 자동 fallback하지 않는다**. 기존 NEXO에 그대로 적용하면 현재 HUD 경로를 잃는다.

`hud_wifi_worker.authenticate()`는 nonce/HMAC 상호 인증, 레코드는 AES-GCM과 순서 검사다. `Runtime.sender()/receiver()`는 7716의 snapshot/media 두 TCP 채널, 유효기간, bounded send/ACK, 네트워크 재탐색·재접속을 구현한다. `hud_wifi_route.select_link()`는 전용 WLAN을 우선하고 실제 경로의 인터페이스를 검사한다. 연결 실패가 추론 deadline을 막지는 않지만 초기 USB 키 협상 실패 후에는 새 USB 세션이 필요할 수 있다.

### waiting_for_comma_wifi의 의미

고정 Jetson `wifi_apply.connect()`에서 연결 가능한 전달 프로필이 없으면 `waiting_for_comma_wifi`를 반환한다. worker 상태 파일 `/dev/shm/carrot-jetlink-network-status.json`→`boot_status.collect()`→`boot_display.lines()`의 **Wi-Fi 행**으로 표시된다. NCM의 ping/5599 listener나 NEXO HUD snapshot 준비 조건은 아니다. 이 글자를 없애기 위해 정상 Wi-Fi나 boot display를 바꾸지 않았다. 실제 부팅 화면 코드가 이 고정 버전과 같은지는 설치 SHA 대조가 필요하다.

## 이번 후속 변경

| 파일 | 변경 |
|---|---|
| `openpilot/tools/jetson/ncm_display.py` | `nexo_display_ack_v1` 협상. host는 snapshot 저장 성공 후 마지막 HUD sequence와 자체 receipt age를 STATE에 반환. client `HudReceipt`가 같은 ordered TCP stream의 마지막 HUD sequence와 일치하는지 검사. 누락/이전/미래/잘못된 ACK는 기존 cleanup/2초 retry로 연결. |
| 같은 파일 | `DisplaySession.check_alive()/receive_session()`: HELLO 3초, 완전한 유효 메시지 없는 세션 5초 만료. 부분 바이트/recv timeout은 시간을 연장하지 않음. 반복 HELLO 거부. transport close 오류에도 navi와 stale snapshot 회수. |
| 같은 파일 | capability 없는 PR #30 peer는 기존 호환 경로 유지, `hud_ack_supported=false`, `hud_rx_confirmed=false`. 새 peer는 host age + 전체 STATE 왕복 시간으로 0.5초 신선도 판단. 원격/로컬 monotonic 절대값 비교 없음. |
| `openpilot/tools/jetson/trt_diagnostics.py` | 기존 읽기 전용 설치 revision/서비스/TRT 수집기에 `iw phy`, `iw dev` 추가. capability/현재 topology만 조회. 명령이 없어도 오류만 기록하고 수집 계속. |
| `openpilot/tools/jetson/tests/test_ncm_display.py` | 실제 localhost TCP 새 세션, ACK/legacy, 지연·오류·만료, 재시도와 자원 회수 회귀 테스트. |
| `openpilot/tools/jetson/tests/test_trt_diagnostics.py` | 허용된 read-only Wi-Fi 명령과 미설치 iw 처리 검사. |
| 이 문서 | 최신 소스 함수 비교, 전용 Wi-Fi 설계, 기능/실기 검증 구분. |

HUD ACK는 Jetson snapshot 파일에 저장됐다는 확인이다. **TURZX 실제 화면 출력, 모델 준비, AI 추론 성공을 뜻하지 않는다.** 패널 출력은 기존 `hud-status.json` 및 실제 화면으로 별도 확인한다. STATE/producer가 중단되면 기존 client supervisor가 종료·재시도한다. 이 변경은 추론 전송/프로토콜, 모델, 네트워크 구성, 보호형 installer, HUD layout 파일을 수정하지 않는다.

## 단계별 상태

| 구분 | 내용 | 현재 상태 |
|---|---|---|
| NEXO에 이미 구현 | g1/ADB 보존 NCM, comma 주소 없는 NIC 복구, 추론 OFF 네트워크 worker, USB 경로 검증, 보호형 transaction/rollback, NEXO ONNX 계약 | NEXO 소스에 존재, 장비 적용 SHA는 미확인 |
| 선행 PR #30 | Jetson IPv4 빈 배열 복구, HUD-only opt-in, probe 단계 분리, 기존 boot display panel lock 공유 | 코드/패키지 검증 완료, 미병합·실차 미검증 |
| 이번 후속 | NCM HUD ACK·상대 age·TCP 지연 반영, 표시 세션 deadline/retry 보강, Wi-Fi capability read-only 수집 | feature branch 구현/자동 시험 |
| 당근에 있고 NEXO에 없음 | nav 전용 virtual AP, private Wi-Fi provisioning, session key 협상, 암호화된 별도 WLAN HUD 채널 | 기능 설계·재사용 후보. wip/고정 Jetson main에는 nav AP/HUD WLAN 모듈이 없음 |
| 실제 Jetson에 아직 반영되지 않은 것으로 보고됨 | NEXO NCM protected release, 표시 전용 HUD host 및 위 복구 | 사용자의 rolled_back 보고 기준. 실제 current 파일 해시 대조 전 단정 금지 |
| 아직 해결하지 못함 | TRT 10.3 FP16 Conv+GELU engine build | 정확한 GPU root cause 미확정. 기존 상세 진단·rollback 유지. 성공으로 표시하지 않음 |
| 추가 필요 | AP/STA 동시 동작·경로/채널 보존 확인, 승인된 키 provisioning, NCM/USB 양쪽 제어채널 capability, WLAN HUD 선택기와 rollback | 독립 후속 구현·하드웨어 승인/검증 필요 |

TensorRT는 실기 로그상 engine build 단계 실패다. ONNX 사전 검사 성공과 HUD import 성공은 GPU 엔진/실제 패널 검증이 아니다. 실제 NEXO FP16 depthwise Conv(group64, stride2)+tanh GELU는 모델 그래프 회귀를 유지한다. 모델 SHA `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`, 입력/출력 계약과 수치 연산은 그대로다. `trt_diagnostics.instrument_builder()`의 bounded verbose capture를 재사용하고 동일 설치·GPU build를 자동 반복하지 않는다.

## 전용 Wi-Fi 후속 설계 — 아직 실행 경로에 추가하지 않음

1. **관찰:** 양쪽 실제 revision/USB 경로/PHY valid interface combinations/채널/경로를 수집한다. comma4가 managed+AP를 동시에 허용하는지 확인한다. `iw` 부재는 capability 미지원 판정이 아니다.
2. **명시적 offroad 설정:** 별도 opt-in과 관리자가 승인한 설정으로 소유 AP/profile을 만든다. 기존 wlan0 프로필·기본 경로·DNS 변경은 금지한다. 겹치지 않는 subnet을 사용하고 필요 없는 Carrot internet-sharing/NAT 정책은 가져오지 않는다. regulatory domain/동일 PHY 채널 제한을 지켜야 한다.
3. **협상 분리:** g1을 건드리지 않고 현재 USB 케이블의 NCM control session에서 별도 versioned capability를 협상하는 어댑터를 설계한다. 현재 NEXO NCM은 0x4002 등 관리 요청을 거부한다. 이 allowlist를 통째로 완화하지 않는다. 전용 AP 1개의 소유 프로필만 허용하고 임의 saved Wi-Fi 복제는 도입하지 않는다. 읽기 전용 검토를 마친 뒤 protected DATA/권한/transaction으로 관리한다.
4. **HUD 채널:** 검증된 Carrot `hud_wifi_worker`의 bounded authenticated channel, ACK, route selection, parent lifetime 원칙을 재사용한다. 세션 키를 argv/로그/Params에 넣지 않는다. NEXO snapshot/preview/media 형식과 renderer는 유지한다. 처음부터 두 송신자가 같은 snapshot을 덮어쓰지 않도록 단일 generation/선택기를 둔다.
5. **기존 경로 유지:** `ncm`이 기본. 실기 검증과 명시적 opt-in 후에만 `wifi` 선택. Wi-Fi 장애 시 이전 generation 만료→worker 종료→검증된 NCM으로 복귀하는 상태 전이를 먼저 테스트한다. upstream nav의 'USB fallback 없음' 정책을 그대로 가져오지 않는다. modeld/차량 제어는 이 전이를 기다리지 않는다.
6. **부팅·복구:** 최초 설치 후에는 별도 수동 명령 없이 manager/systemd가 시작·재시도한다. AP 생성은 offroad에서만, 장비 인식 지연/이름·ifindex 변경을 재검증한다. onroad AP 재생성 지연은 현재 NCM 유지로 처리한다. 최초 USB 키 협상은 inference OFF의 표시 전용 세션으로도 가능하도록 해야 한다.
7. **적용 판정:** 정차 중 단독/동시 재부팅, Wi-Fi uplink 변화, USB/HUD 탈착, AP 채널 변화, 두 CPU/GPU 부하, frame age/ACK/drop/retry를 측정한다. 성공 전에는 기존 NCM을 삭제하지 않는다. 임의 AI 활성화는 없다.

이 설계는 현재 코드와 충돌하는 provisioning 권한·service 수명·세션 선택기 변경을 포함하므로 이번에 구현하지 않았다. 원래 장기 목표의 USB inference는 g1을 보존하는 NCM-TCP로도 구성할 수 있으며, 반드시 별도 `1209:0001` Bulk gadget이어야 하는 것은 아니다.

| 항목 | 현재 NCM-TCP | 향후 nav형 전용 Wi-Fi HUD |
|---|---|---|
| 배선 | 현재 USB 케이블 + HUD USB-A, 공유기 불필요 | 배선 동일, 공유기 불필요 |
| g1/ADB | 기존 gadget 재사용 | AP 자체는 충돌 없음. upstream Bulk provisioning을 그대로 쓰면 g1 문제는 남음 |
| 추론과 HUD 부하 | inference 모드의 HUD가 같은 USB stream을 공유; 표시 전용은 inference OFF | HUD payload/재시도가 별도 WLAN worker. 무선·CPU 부하와 초기 USB 협상 의존성은 남음 |
| 자동 복구 | 주소/peer route/ifindex/TCP listener와 이번 ACK 감독 | AP·NM 프로필·DHCP·키·두 TCP 채널의 추가 복구 필요 |
| 호환성 | 현재 고정 v2/NEXO 계약 유지 | nav capability, crypto dependency, protected runtime version 조율 필요 |
| 위험/검증 | 실제 USB 재열거/권한/양쪽 boot 미검증 | STA/AP firmware, 채널 경쟁, 인터넷 경로, 키 수명·fallback·성능까지 추가 실기 검증 |

## 자동 검증과 실차 미검증

최종 전체 회귀: **535 passed, 4 skipped, 0 failed**, 경고 19개. 선행 PR #30 기준 508개에서 27개 회귀를 추가했다. 실제 NEXO ONNX/고정 Carrot builder를 사용한 CPU 회귀, 실제 localhost TCP 연결, simulated network/systemd/rollback, 상태 UI 호환 검사를 실행했다. 변경 Python 4파일 Ruff와 Python 3.10 구문, diff whitespace 검사를 통과했다. GPU/systemd/USB 하드웨어를 CPU 대역 결과로 대신 주장하지 않는다.

첫 전체 실행에서는 533 passed/4 skipped/1 failed였다. 실패는 변경하지 않은 installer의 Windows 임시파일 `os.replace` WinError 5였다. 별도 sandbox 실행에서도 임시 디렉터리 생성 권한 오류가 발생했다. production 설치 로직이나 테스트 assertion을 바꾸지 않고 정상 권한의 새 임시 디렉터리에서 534 passed/4 skipped/0 failed로 재확인했다. 이후 STATE 왕복 지연 신선도 테스트를 한 개 더 추가했다. 최초 실패 결과도 보존했다.

4 skip은 pyray 1개, 빌드된 Linux cereal/msgq runtime 2개, 실제 Linux flock 1개다. pytest/aiohttp 경고 19개는 별도 남는다. 변경 Python 파일 Ruff/Python 3.10 구문과 whitespace 검사를 실행한다.

| 현장 항목 | 판정 |
|---|---|
| comma만 / Jetson만 / 동시 전원 재인가, USB 분리·재연결, NIC 이름 변경 | 자동화 상태 재현 통과 대상, **실차 미검증** |
| g1/ADB 유지, 기존 Wi-Fi·기본 경로 보존, Jetlink OFF | 코드/회귀 확인, 실제 장비 동시 동작은 **실차 미검증** |
| TURZX `1cbe:0092` boot 화면 인계, HUD TX, 메뉴/카메라/주차센서 | renderer/layout 변경 없음, 실제 패널/차량 데이터는 **실차 미검증** |
| TensorRT 10.3 engine build·수치·지연 | 사용자 실기 실패 이후 성공 증거 없음, **실차 미검증/미해결** |
| 전용 Wi-Fi AP, 암호화 채널, NCM fallback, 주행 중 안정성 | 설계 단계, **미구현·실차 미검증** |

## 집에서 수행할 읽기 전용 확인

아래 명령은 사용자가 관찰할 때만 사용한다. 이번 작업에서는 장비에서 실행하지 않았다. Git 변경/설치/프로필 활성화 명령이 아니다.

Jetson:

```sh
readlink -f /opt/carrot-jetlink/current
cat /opt/carrot-jetlink/current/SOURCE_COMMIT
cat /opt/carrot-jetlink/updates/nexo-install-status.json
systemctl show carrot-jetlink carrot-jetlink-hud nexo-jetson-ncm -p ActiveState -p ExecStart -p MainPID
lsusb
ip -j link show
ip -j -4 addr show
ip -j -4 route show
ss -ltnp 'sport = :5599'
iw phy
iw dev
```

comma의 기존 checkout:

```sh
git status --short
git rev-parse HEAD
ip -j link show dev usb0
ip -j -4 addr show dev usb0
ip -j -4 route show exact 192.168.100.250/32
test -e /data/nexo_jetlink_enabled && echo INFERENCE_ON || echo INFERENCE_OFF
cat /dev/shm/nexo-jetson/ncm-display.json
iw phy
iw dev
```

새 수집기 파일을 별도로 전달받은 경우에만 `sudo python3 trt_diagnostics.py --collect /opt/carrot-jetlink/updates/nexo-observation-$(date +%Y%m%d-%H%M%S)`로 새 진단 디렉터리를 만들 수 있다. 수집기는 서비스/네트워크/GPU 상태를 바꾸지 않는다. 기존 ZIP에는 이번 후속 ACK/`iw` 수집이 들어 있지 않으며 ZIP을 새 버전인 것처럼 재명명하지 않는다.

추후 적용에는 양쪽 feature 코드, Jetson protected release와 최초 표시용 opt-in이 필요하다. 이전 설치 절차·rollback은 [PR #30 검토 문서](jetson_ncm_hud_recovery_20261021.md)를 따른다. 미검증 TensorRT를 정상 설치로 우회하지 않는다. 차량의 약 41개 로컬 수정은 백업·파일별 대조와 별도 승인 없이 pull/reset하지 않는다. 이번 결과는 코드 검증과 feature 게시이며 실차 설치 완료가 아니다.
