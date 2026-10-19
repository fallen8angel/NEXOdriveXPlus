# NEXO Jetlink NCM-TCP 정밀 검토 — 2026-10-20

> PR #28의 기존 구현을 이어 수정했다. 실험 기능은 기본 비활성화이며 NEXO 본 브랜치에는 병합·푸시하지 않는다.
> 아래 결과는 소스·로컬 테스트에 대한 것이다. 콤마4/Jetson/TURZX에 접속하거나 설치·재시작·주행 시험을 하지 않았다.

## 1. 확인한 저장소와 기준

| 대상 | 확인한 커밋 | 용도 |
|---|---|---|
| fallen8angel/NEXOdriveXPlus NEXO | a880d27273107f925e9222ac2a4dcd15a59cfddd | 변경 기준 |
| 기존 PR #28 | 09cc31e7af58b1845a3ce92b099c4bec02a908b2 | 기존 11개 커밋, 8개 변경 파일을 재사용 |
| ajouatom/openpilot carrot-wip | ba186915179816a9b0476cec52bf321e73d2d4b0 | 작업 시작 시 최신 비교 소스 |
| ajouatom/carrot-jetson main | d0dc96fa32410403dfb2fefff3e2909c11ec109b | 현재 설치 스크립트가 고정한 서버와 동일 |
| zoompilot/jetlink main | d24328d849b0e5154b55f149dc18c5dfe8bd79cc | 최신 원본, 프로토콜 v3 |

작업 브랜치: `fix/jetlink-usb-g1-diagnostics-20261019`.
PR: https://github.com/fallen8angel/NEXOdriveXPlus/pull/28 (Draft 유지).
원본 Jetlink의 Windows 금지 문자(:)를 포함한 테스트 fixture 때문에 전체 checkout은 실패했지만,
필요한 protocol/TCP 파일은 해당 커밋의 Git 객체에서 직접 읽었다. 이 실패는 제품 테스트 결과가 아니다.

## 2. 연결 실패 원인: 확인과 미확인의 구분

### 제공된 실차 로그로 확인되는 사실

- 유일한 UDC `a600000.dwc3`를 AGNOS `g1`이 사용하고 있었다.
- `g1`에는 `ffs.adb`, `ncm.0`이 있고 ADB가 활성화되어 있었다.
- 별도 `jetlink` FunctionFS gadget 생성 절차가 점유 충돌을 발견하고 종료했다.
  따라서 이 시점의 `setup_gadget.sh exit 1`과 `retrying`의 직접 원인은 UDC 점유 충돌이다.
- `configured`, `usb0 UP,LOWER_UP`, RX/TX 기록은 USB/NCM 구성 및 일부 통신의 증거다.
  모델 SHA 검증, TensorRT 준비 또는 HUD 표시 성공의 증거는 아니다.
- IPv6 주소에서 SSH `Permission denied (publickey)`가 나온 것은 그 시점에 SSH 서버가
  공개키 인증 단계까지 응답했다는 뜻이다. 서버가 실제 Jetson인지 호스트키·현장 식별로 확인해야 한다.
- 이후 timeout/unreachable은 그 이후 응답이 끊겼다는 사실만 보여 준다.
- 콤마에 임시 IPv4를 넣은 뒤 경로가 usb0로 바뀐 것과 대상 ping 무응답은 별개의 관측이다.
  Jetson에 실제 `192.168.100.250`이 설정돼 있었다고 확정할 수 없다.

### 현재 확정할 수 없는 것

Jetson OS 부팅 실패, 네트워크 관리자의 주소 제거, 서비스 중단, 케이블 접촉, 전원 불안정,
IPv6 이웃 만료 중 어느 것이 후속 단절을 일으켰는지는 양쪽 동시간 로그가 없어 구분할 수 없다.
팬 회전은 부팅 완료 증거가 아니며, 모니터 무출력도 단독으로 OS 부팅 실패를 확정하지 못한다.
MAC/IPv6 주소는 장치의 신원 인증이 아니다. 현재 NCM 상대가 Jetson인지는 직접 확인해야 한다.

## 3. 당근과 원본 Jetlink의 처리 방식

### USB 생성 순서와 g1

`tools/jetlink/setup_gadget.sh`는 configfs/UDC를 확인하고 다른 gadget이 UDC를 점유하면 중단한다.
이후 jetlink VID/PID, config, ffs.jetlink를 만들고 FunctionFS를 mount한다.
이 단계에서는 UDC에 바인딩하지 않는다. FFS transport가 ep0에 descriptor를 쓴 다음 UDC를 바인딩한다.
서버는 Jetson USB host에서 `1209:0001`을 찾아 bulk endpoint를 연다.

**최신 carrot-wip는 plain USB만 있는 버전이 아니다.** `mobile.py`의 기본 auto/ios 경로는
`setup_mobile.sh gadget`으로 FFS+NCM composite 구성을 만든다. 이 helper 역시 먼저
`setup_gadget.sh`를 호출하므로 점유 중인 g1을 공존·합성 대상으로 받아들이지 않는다.
자기 소유 `ncm.jetlink`만 관리하며, 바인딩 후 네트워크/DHCP와 INPUT 격리 규칙을 준비한다.
콤마가 `192.168.60.1:5599`에서 TCP를 수신하는 mobile 경로이며 이번 NEXO의
“기존 g1을 보존하고 콤마가 Jetson TCP 서버로 접속”하는 방향과 다르다.

auto는 TCP에 5초를 주고 USB v3/v2 HELLO를 순서대로 시도하며, 실패 후 NCM에 더 긴 유예를 준다.
동시에 두 transport가 같은 세션을 제어하는 형태가 아니다. plain USB/Android 경로도 유지된다.
이 전체 구조나 USB 전원 제어를 NEXO로 복사하지 않았다.

### 실차 시험 근거의 한계

당근 공개 문서에는 정차한 C4의 120초 관측과 inference/HUD를 재시작하지 않은 네트워크 복구 사례가 있다.
그러나 그 기록에 AGNOS g1의 ffs.adb+ncm.0를 유지했는지 판별할 gadget 목록은 없다.
따라서 “당근 실차에서 ADB+g1을 유지한 채 이번 구성과 동일하게 성공했다”고 결론 내릴 수 없다.
소스가 보여 주는 것은 자체 Jetlink gadget의 소유권 규칙이다.

### Bulk와 TCP

| 항목 | USB Bulk | 이번 NCM-TCP |
|---|---|---|
| 콤마 역할 | FunctionFS USB device | 기존 g1의 NCM network device |
| Jetson 역할 | libusb host | cdc_ncm network host + TCP server |
| g1 점유 | 별도 gadget과 충돌 | g1·ADB·NCM을 그대로 사용 |
| 주소 | IP 불필요 | 양쪽 USB IPv4와 직접 경로 필요 |
| wire | pinned JLNK v2 + bulk 정렬 | 같은 v2 header/flags, TCP stream 분할 재조립 |
| 연결 복구 | 장치 재검색/endpoint 재개방 | 제한시간 후 새 socket, 매 연결 시 topology 재검증 |
| 지연 | USB/스케줄링 영향 | 위 영향 + TCP 버퍼/재전송 영향, 실측 필요 |

원본 최신 Jetlink는 v3다. 이번 NEXO와 고정 carrot-jetson은 v2이므로 최신 원본을 그대로
갈아끼우면 호환된다고 볼 수 없다. v2 32-byte header, 메시지 번호, 1024-byte 경계의 PADDED 처리와
실제 서버 왕복을 검사했다. 원본 TCP 설명의 “AGNOS host-side USB Ethernet driver 부재”는
AGNOS가 호스트가 되는 반대 구성을 말한다. 이번 구조에서는 Jetson이 cdc_ncm 호스트다.

당근 USB-C hotfix는 P3768/L4T 36.4.7의 C-to-C FUSB301 역할 설정용이다.
사용자의 USB-A host 배선에 필요한 수정이라는 근거가 없으므로 적용하지 않았다.

## 4. 기존 PR 및 관련 연결 코드에서 발견한 문제와 수정

### 재현한 실제 코드 결함

1. **실제 NEXO 출력 계약 오류:** ONNX의 `pad` slice가 `(-2, None)`인데
   `ModelSpec.output_nelem`이 모든 stop을 int로 변환해 TypeError가 발생했다.
   wire 출력 길이는 slice 끝값이 아니라 `output_shapes['outputs']`로 계산하도록 수정했다.
   실제 파일 메타데이터로 이전 식의 실패와 수정 후 2,576 float 처리를 함께 테스트했다.
   이 오류는 PR #28 이전 코드에도 존재했고 이번 요청의 모델 호환성 검토에서 발견했다.
2. **전체 송신 기한 누락:** 기존 기본 0.5초는 개별 send 호출 기한이어서
   부분 송신이 계속 진행되면 메시지 전체 시간은 제한되지 않았다.
   명시적 timeout 없는 HELLO/STATE/서버 응답에도 메시지 전체 0.5초 deadline을 적용했다.
   기한 내 완료되지 않은 부분 송신 stream은 닫는다.
3. **서버 송수신 timeout 공유:** pinned Carrot read-ahead가 수신하면서 송신 thread가 같은
   socket.settimeout을 바꾼다. 두 방향의 cached timeout과 실제 socket mode가 달라질 수 있었다.
   NCM client/server는 고정 nonblocking socket과 독립 select deadline을 사용한다.
4. 기존 NCM 테스트 5개 중 Windows에서 2개가 실패했다. `/sys/class/net/lo` 전제와
   Windows에 없는 `sendmsg` 때문이다. 이는 실차 실패 증거가 아닌 테스트 이식성 문제다.
   fake sysfs와 Windows의 부분 send 대체 경로로 실제 socket 통합 시험을 가능하게 했다.

### 코드상 취약점·누락을 보완한 항목

- 경로 검사 + IP bind만으로 연결 후 경로 변경을 차단하지 못함:
  client 및 server listener/accepted socket에 `SO_BINDTODEVICE`를 적용했다.
  실패하면 연결·활성화를 거부하고 다른 interface로 우회하지 않는다.
- client는 실제 활성 gadget NCM ifname, UP 및 /24 주소, 직접 route를 읽기 전용으로 확인한다.
  server는 명시적으로 지정한 cdc_ncm interface와 반대 방향 주소/route를 확인한다.
  일반 USB Ethernet/Wi-Fi adapter를 단지 sysfs 경로에 usb가 있다는 이유로 받아들이지 않는다.
- 서버는 지정 USB 주소/5599에만 bind하고 comma 소스 주소만 허용한다.
  매 재연결 시 preflight를 다시 한다. listener timeout은 늦은 부팅/재연결 대기를 유지한다.
- NCM 서버는 HELLO/ENGINE/INFER/STATE/PING/HUD/NAVI만 허용한다.
  upstream upload, shutdown 및 Wi-Fi provisioning 등 다른 메시지는 차단한다.
  ENGINE_REQ도 설치한 NEXO 모델의 SHA/파일 크기/frame_skip과 일치해야 한다.
- malformed TCP 연결은 즉시 닫아 upstream bulk drain의 연속 수신 시 무기한 대기를 피한다.
- 서버 수신 1 MiB 제한, 불완전 메시지/유휴 peer 60초 상한을 적용했다.
  60초는 기존 엔진 준비 기한보다 길게 두었다. 무응답 상대가 단일 서버를 영구 점유하지 않도록 한다.
- 설치 시 검증한 콤마 SHA와 Jetson ONNX가 다르면 서비스 변경 전 중단한다.
  NCM adapter는 검증한 d0dc96fa 서버 revision만 허용한다.
- 늦은 NIC/IP 준비로 systemd 시작 제한에 영구 정지하지 않도록 서버 unit의 시작 횟수 제한을 해제했다.
  기존 2초 restart 간격으로 재시도한다. 주소나 route를 자동 생성하지는 않는다.
- NCM 활성화 전 동일 Python 실행 환경의 interface bind 권한을 점검한다.
  이미 활성화된 실험 세션은 먼저 offroad disable하도록 요구한다.
- ready/retrying 진단에도 선택 transport를 남기고, Jetson용 읽기 전용 진단을 추가했다.
- 오래된 Cinque 오류 문구와 당근 plain-only 설명을 바로잡았다. 모델 파일은 변경하지 않았다.

보안의 한계: IP/interface 제한은 **암호학적 인증이 아니다**. 케이블 반대편의 악성 호스트나
소스 위조까지 신뢰할 수 있는 것으로 만들지 않는다. 신뢰된 전용 USB 연결의 정차 실험만 전제한다.
공유 LAN/Wi-Fi 노출 또는 인증이 필요한 운영 환경에는 별도의 인증 설계가 필요하다.

## 5. 실제 NEXO 모델 계약

저장소 파일을 직접 해시하고 pinned 서버의 ONNX parser로 읽은 결과:

- SHA-256: `f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5` (제공한 차량 SHA와 일치)
- 파일: 60,792,584 bytes. frame_skip: 4.
- img / big_img: 각각 `[1,12,128,256]`.
- desire_pulse `[1,25,8]`, traffic_convention `[1,2]`, action_t `[1,2]`, features_buffer `[1,24,512]`.
- 전송 영상: `[2,6,128,256]` uint8 = 393,216 bytes.
- packed context: float32 524개 = 2,096 bytes.
- INFER_REQ payload: 8-byte frame header 포함 395,320 bytes.
- outputs: `[1,2576]` float32 = 10,304 bytes; INFER_RESP payload는 timing header 포함 10,324 bytes (선택 telemetry 제외).

ONNX 파일/metadata의 일치와 wire 호환은 확인했다. **실제 Jetson의 TensorRT plan이 이 모델로
빌드됐는지, 정확한 GPU 출력과 지연이 나오는지는 확인하지 못했다.** 설치는 정확한 ONNX로
엔진을 준비하고, 세션에서 전체 spec 비교와 두 번의 reset warmup을 통과해야 ready가 된다.
다른 모델이나 계약 불일치 시 연결 준비가 실패하며 로컬 모델이 유지된다.

이미 원격 추론이 active인 상태의 실패는 기존 JoiningModel이 fault를 기록하고 modeld 재시작을
요청하는 구조다. “주행 중 끊겨도 지연 없이 로컬로 전환된다”고 보고할 수 없다.
이번 작업은 그 차량 제어/전환 정책과 150 ms frame deadline을 변경하지 않았다.

## 6. 변경 파일

| 파일 | 변경 |
|---|---|
| modeld/jetlink/ncm_transport.py | JSON topology preflight, interface 고정, 독립 nonblocking I/O, 전체 송신 기한 |
| tools/jetson/ncm_server.py (신규) | pinned TCP adapter, source/메시지/모델 제한, 재접속·유휴·길이 제한 |
| tools/jetson/native_host.py | 명시적 NCM 모드에서만 위 adapter 장착, 실행 시 모델 SHA 확인 |
| modeld/jetlink/spec.py | 실제 ONNX output shape로 wire 크기 계산 |
| modeld/jetlink/client.py | NEXO 모델 오류 문구 정정 |
| modeld/jetlink/daemon.py | transport 선택 함수와 ready/retrying transport 진단 |
| modeld/jetlink/install_jetson_server.sh | interface·SHA·revision preflight, 서비스 한정 CAP_NET_RAW, 늦은 부팅 retry |
| modeld/jetlink/nexo_jetlink_ctl.sh | 공통 preflight 재사용, 실행 중 mode 교체 거부 |
| modeld/jetlink/diagnose_usb_owner.py | Jetson 주소·드라이버·route·service·모델 SHA 읽기 전용 검사 |
| modeld/tests/test_jetlink_ncm_transport.py | topology·부분 패킷·padding·deadline·EOF·g1 보존 검사 |
| modeld/tests/test_jetlink_ncm_server.py (신규) | pinned 서버/실제 ONNX 계약/실제 TCP/모의 GPU 통합 검사 |
| modeld/jetlink/README.md, USB_G1_COLLISION_KO.md, 이 보고서 | 비교·설정 요구사항·검증 한계 |

경로의 modeld는 `openpilot/selfdrive/modeld`, tools는 `openpilot/tools` 아래다.
차량 MED/SCC/레이더/CAN/DBC/조향/정지선/R-D/DM, 모델 ONNX, HUD layout, YOLO 프로그램은 수정하지 않았다.

## 7. 테스트 결과

최종 관련 회귀: **260 passed, 4 skipped, 1 deselected** (Windows Python 3.12).

- active NCM/UP/주소/직접 route 검사, Wi-Fi·gateway·다른 source 거부.
- interface bind 권한 실패 시 dial하지 않음. 장치 pinning → source bind → connect 순서.
- 실제 socket에서 0/1/991/992/993/395,320-byte payload, header 분할 timeout 후 재개, EOF,
  malformed header, 길이 초과, 부분 송신 전체 기한, 읽지 않는 peer의 송신 정체.
- pinned Carrot 실제 Session/PolicyQueues/ReadAheadTransport 및 실제 TCP loopback을 사용해
  HELLO → exact ENGINE → 395,320-byte 입력 → 2,576개 출력 → HUD → 다음 추론을 검증.
  GPU만 NumPy 대역이며 실제 TensorRT 실행은 아니다. 새 연결 3회에서 반복했다.
- 다른 모델 요청 후 client.spec가 None인지, upload/shutdown/Wi-Fi 메시지 차단,
  listener 재사용과 다른 source 거부, 유휴·oversize 제한.
- 기존 HUD 순서·12 ms tail budget·종료/재연결/서비스 관리/7000 API/CSRF 회귀 포함.
- 기존 test 5개 실행의 2실패는 위 Windows 전제에서 재현하고 수정했다.
  새 송신 정체 시험도 Windows 수신 buffer가 첫 1 MiB를 받아들이는 탓에 첫 시도 실패했으며,
  bounded buffer를 실제 포화시킨 뒤 deadline을 검증하는 형태로 수정해 통과했다.
- Python Ruff, Python 3.10 문법, Bash 구문, git diff whitespace 검사를 통과했다.

skip 4개: pyray 1, built Linux cereal/msgq 2, Linux flock 1.
제외 1개: 기존 protected-install rollback의 Windows directory rename WinError 5 사례.
기존 aiohttp 및 pytest C++ plugin 설정 경고 19개가 남았다.

재현 명령(테스트 의존성 및 CARROT_JETSON checkout을 준비한 환경):

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONUTF8=1 CARROT_JETSON=<pinned checkout>
python -m pytest -p pytest_asyncio.plugin -o asyncio_mode=auto -o addopts='' --noconftest \
  openpilot/selfdrive/modeld/tests/test_jetlink_ncm_transport.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_ncm_server.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_network.py \
  openpilot/selfdrive/modeld/tests/test_jetlink_display.py \
  openpilot/tools/jetson/tests -q --tb=short -rs \
  -k 'not test_service_start_failure_rolls_back_after_candidate_selection'
```

미실행: 전체 openpilot 빌드, Linux kernel의 실제 SO_BINDTODEVICE/권한·interface 분리,
실제 cdc_ncm/AGNOS ADB 지속 연결, ip/systemd 명령 실물 적용, TensorRT 빌드·출력 비교,
USB 케이블 탈착/전원 반복/장시간 부하, 실물 TURZX 출력, 실차 주행.
GitHub CI 성공이나 실차 연결 성공으로 대체해서 해석하면 안 된다.

## 8. AI·HUD·YOLO 동시 사용 판단

코드 구조와 모의 통합 시험상 AI와 HUD는 동일한 JLNK 세션에서 공존할 수 있다.
AI 결과를 modeld에 보낸 뒤 최대 12 ms의 bounded tail로 HUD/NAVI를 전송한다.
화면 생성은 낮은 우선순위의 별도 worker가 담당한다. 서버 read-ahead queue는 2개로 제한된다.
이 구조가 HUD 부하를 없애는 것은 아니다. TCP buffer·tail·GPU/CPU/USB controller 공유로
다음 frame과 지연 분포에 영향을 줄 수 있으며 실제 동시 부하 측정이 필요하다.

콤마↔Jetson은 NCM interface이고 TURZX는 별도 `1cbe:0092/0123` USB device다.
NCM 모드의 Jetlink server는 TURZX libusb 장치를 열지 않는다. HUD renderer는 기존 `hud.lock`을 사용한다.
설치 스크립트는 중복 소유 가능성이 있는 대체 host/carrot HUD service만 중지하고
`nexo-yolo.service`를 삭제·중지·대체하지 않는다. YOLO의 GPU/VRAM 사용량은 여전히 추론과 경쟁할 수 있다.

따라서 결론은 **소프트웨어상 공존 경로 및 wire 동작은 검증, 실제 세 장치의 동시 안정 작동은 미검증**이다.

## 9. 실차 적용 전 순서와 복구

1. 정차/주행 보조 해제 상태에서 Jetson의 LAN 또는 별도 검증된 SSH 접근을 확보한다.
   호스트키/hostname/OS 정보를 현장 장치와 대조하고, NCM IPv6 상대가 Jetson인지 식별한다.
2. 양쪽에서 아래 읽기 전용 진단과 시간대가 일치하는 kernel/service 로그를 수집한다.
   `configured`와 실제 interface carrier·이웃·주소 변화, 서버 종료 시간을 함께 비교한다.
3. Jetson에서 콤마에 연결된 **cdc_ncm** interface를 확인한다. 기존 `.250` 주소를 추정하지 않는다.
   이번 실험의 고정 주소쌍을 사용하려면 양쪽에 명시적으로 설정해야 한다.
   코드가 주소를 자동 부여하지 않으며 기존 임시 ip addr 설정의 재부팅 지속성도 보장하지 않는다.
4. NetworkManager 또는 해당 OS의 실제 네트워크 관리자를 확인한 후 그 USB interface에 한정된
   지속 profile을 별도 검토한다. default route/DNS를 바꾸지 않고 USB detach/reboot 이후 주소·직접 route를 다시 확인한다.
   g1 MAC이 재생성될 수 있으므로 관측한 MAC 한 개를 무조건 영구 장치 ID로 쓰지 않는다.
5. 콤마 실행 계정의 SO_BINDTODEVICE preflight를 확인한다. 권한이 부족하면 기능은 거부된다.
   일반 Python 바이너리에 setcap하거나 실패 시 Wi-Fi로 우회하지 않는다.
   Jetson unit에는 이 기능에 필요한 CAP_NET_RAW만 부여하며 실제 L4T/systemd 호환성을 확인해야 한다.
6. 양쪽 동일 SHA를 검증한 뒤 Jetson에 해당 ONNX로 TensorRT plan을 준비한다.
   기존 엔진 sidecar의 전체 spec/환경 호환, 출력 정확도, warmup, P50/P95/P99/최대 end-to-end 지연을 검증한다.
7. 별도 브랜치를 양쪽에서 검토 배포한 후에만 NCM opt-in을 선택한다. 이번 작업에서는 활성화하지 않았다.
   기존 Jetlink가 켜져 있으면 offroad disable 후 선택한다. ready와 active, HUD 실제 화면을 따로 확인한다.
8. 정차 상태에서 USB 탈착, Jetson 늦은 부팅, 서버 restart, comma reboot, IP 소실, HUD 탈착,
   YOLO+HUD+AI 동시 부하를 시험한다. ADB/NCM 원격 접속 유지와 중복 renderer 부재도 확인한다.
9. 실패 시 offroad `nexo_jetlink_ctl.sh disable`로 marker/mode를 제거하고 로컬 모델로 복귀한다.
   필요하면 Jetson의 설치 전 unit backup을 검토해 복구한다. g1을 unbind/delete하지 않는다.
   기존 USB bulk mode로 돌아가도 g1 점유가 남아 있으면 같은 충돌이 발생한다.

읽기 전용 진단(각 장치의 저장소에서):

```sh
# comma
python3 openpilot/selfdrive/modeld/jetlink/diagnose_usb_owner.py --role comma
# Jetson
python3 openpilot/selfdrive/modeld/jetlink/diagnose_usb_owner.py --role jetson
```

설치에 필요한 명시적 값은 README의 `NEXO_JETLINK_USB_IFACE`와 `NEXO_JETLINK_MODEL_SHA256`를 따른다.
SSH 키/주소 자동 변경이나 임의 gadget 해제는 하지 않는다.

Docs-Not-Needed: docs/user 사용자 가이드는 변경하지 않고 요청받은 연결 검토 보고서와 실험 개발 문서를 갱신했다.

## 비교 소스 링크

- [당근 setup_gadget.sh](https://github.com/ajouatom/openpilot/blob/ba186915179816a9b0476cec52bf321e73d2d4b0/tools/jetlink/setup_gadget.sh)
- [당근 mobile.py](https://github.com/ajouatom/openpilot/blob/ba186915179816a9b0476cec52bf321e73d2d4b0/openpilot/selfdrive/modeld/jetlink/mobile.py)
- [당근 setup_mobile.sh](https://github.com/ajouatom/openpilot/blob/ba186915179816a9b0476cec52bf321e73d2d4b0/tools/jetlink/setup_mobile.sh)
- [고정 서버 TCP 구현](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/third_party/jetlink/jetlink/transport/tcp.py)
- [당근 정차 시험 기록](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/docs/jetson_power_loss_protection.md)
- [USB-C hotfix 대상 설명](https://github.com/ajouatom/carrot-jetson/blob/d0dc96fa32410403dfb2fefff3e2909c11ec109b/tools/jetlink/USB-C-HOTFIX.md)
- [최신 원본 v3 protocol](https://github.com/zoompilot/jetlink/blob/d24328d849b0e5154b55f149dc18c5dfe8bd79cc/jetlink/protocol.py)
