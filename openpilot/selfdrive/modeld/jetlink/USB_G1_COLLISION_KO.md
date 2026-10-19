# 콤마4 ↔ 젯슨 USB-A ↔ 외부 HUD: 2026-10-19 실차 진단 및 수정

> **실험용·기본 비활성화.** 이 문서는 실제 차량의 문제를 바탕으로 작성했습니다.
> USB-NCM-TCP 경로는 새로 구현했지만, 콤마4/Jetson/외부 HUD 실기 추론은 **아직 검증하지 않았습니다**.
> 주행 중 설정·업데이트·테스트하지 마세요.

## 사용자의 실제 배선

- 콤마4 USB-C → 젯슨 USB-A (호스트)
- 젯슨의 다른 USB-A → TURZX 외부 HUD
- 젯슨 별도 전원 공급, 팬 회전. 팬 회전만으로 OS 부팅 완료를 증명할 수는 없습니다.
- HUD는 Jetson의 `nexo-jetson-hud.service`가 별도 USB 장치로 제어합니다. 콤마와 Jetson 사이의 연결 및 모델 추론 준비 여부와 구분해서 검사합니다.

## 10월 19일 확인된 사실

- 콤마4의 유일한 장치 컨트롤러 `a600000.dwc3`를 AGNOS `g1`이 점유했습니다.
- `g1`은 `ffs.adb`와 `ncm.0`을 포함했습니다. `AdbEnabled=1`.
- 기존 JLNK FunctionFS `jetlink` 가젯을 별개로 바인딩하는 단계에서
  `USB gadget 'g1' already owns a600000.dwc3`가 발생했습니다.
- `/sys/class/udc/a600000.dwc3/state = configured`, 콤마 `usb0`는 `UP,LOWER_UP`.
- 상대 USB-NCM 호스트 MAC `a2:ae:7a:18:52:df`가 콤마에서 보였습니다.
- USB IPv6 링크로 `fe80::6b15:550e:2406:ab9a`가 한 번 응답하고 SSH도 공개키 인증까지 도달했으나, 이후 응답이 사라졌습니다.
- 콤마에 USB 주소 `192.168.100.127/24`를 임시 추가하자 목표 `192.168.100.250` 경로는 `usb0`였지만 대상에서 응답하지 않았습니다. 즉 **Jetson USB측 IPv4와 SSH 상태는 여전히 확인 필요**합니다.
- `nexo-jetlink.json` = retrying, 외부 추론 = inactive. 로그의 '정상 후보'는 기본 CAN/제어 진단이지 젯슨 연결 확인이 아닙니다.

## 당근 carrot-wip 비교 (2026-10-20 재검토)

당근의 plain USB 경로는 별도 FunctionFS bulk gadget을 만들고 다른 gadget의 UDC 점유를 거부합니다. 최신 `ba186915`에는 auto/ios용 FFS+NCM composite 경로도 있습니다. 이 경로도 `setup_gadget.sh`를 먼저 실행하므로 점유 중인 AGNOS g1과 자동 공존하지 않습니다. 당근 mobile TCP는 콤마 쪽 listener를 사용하는 구조로, 기존 g1 NCM을 보존하고 Jetson 서버로 접속하는 이번 NEXO와 방향이 다릅니다. 고정된 carrot-jetson `d0dc96fa`와 NEXO는 JLNK v2이며 최신 zoompilot/jetlink의 v3로 임의 교체하지 않습니다. 공개 실차 기록만으로 당근이 동일한 g1/ADB 구성을 유지했는지는 확인되지 않습니다.

당근 `USB-C-HOTFIX.md`는 특정 Orin Nano USB-C ↔ USB-C 역할 문제를 위한 것이므로, 현재 USB-A 호스트 배선에 자동 적용하지 않았습니다. 당근의 Cinque 모델 또한 이식하지 않았습니다. NEXO 고유 ONNX 모델의 SHA와 입력/출력 계약은 기존 검사대로 유지합니다.

## 새로 구현된 코드

- `ncm_transport.py`: `/data/nexo_jetlink_transport`가 **정확히 `ncm-tcp`**일 때만 NCM-TCP를 사용합니다. 기본은 기존 USB FunctionFS입니다. 명시적인 로컬 USB 소스 IP 바인딩, 대상 주소 고정, Wi-Fi·Tailscale·셀룰러 경로 거부, JLNK v2 framing 및 제한시간을 유지합니다.
- `daemon.py`: 명시적인 NCM 모드에서는 `setup_gadget.sh`를 호출하지 않습니다. 따라서 **AGNOS g1/ADB/NCM을 해제하지 않습니다**. HELLO/엔진 해시 검증/워밍업/모델d 연결/기존 HUD 스냅샷은 동일한 경로를 사용합니다.
- `install_jetson_server.sh`: 젯슨에서만 `NEXO_JETLINK_TRANSPORT=ncm-tcp`를 명시하면 서버를 USB측 `192.168.100.250:5599`에만 바인딩합니다. USB 장치에 실제 해당 IP가 있는지 확인하지 못하면 설치하지 않습니다. 기본은 기존 USB bulk 서버입니다.
- `nexo_jetlink_ctl.sh enable-ncm-tcp`: **정차/오프로드 + usb0 IP와 경로를 확인한 뒤에만** 선택합니다. 주소를 자동 부여하거나 재부팅하지 않습니다. `enable`은 기존 USB 전용 모드로 되돌리고, `disable`은 선택을 삭제합니다.
- `test_jetlink_ncm_transport.py`: 모드 선택/잘못된 경로 거부/프로토콜 왕복의 단위·실제 TCP 소켓·고정 Carrot 서버 통합 테스트를 실행했습니다. GPU는 대역이며 실기 통과를 뜻하지 않습니다.

## 정차 중 준비 절차: 먼저 Jetson에 별도로 접속해야 합니다

1. Jetson의 전원·부팅·LAN/Wi-Fi SSH를 확보합니다. 현재 NCM IPv6 SSH는 한 차례 공개키 인증 실패 후 통신이 끊겼으므로 로그인 키를 등록·확인해야 합니다. 개인키는 공유하지 마세요. 화면 출력이 없으면 Jetson과 PC를 LAN으로 연결해 접근하는 방법부터 확보합니다.
2. Jetson에서 `ip -br addr`, `lsusb`, `systemctl status nexo-jetlink-server.service nexo-jetson-hud.service --no-pager`로 상태를 검사합니다.
3. 젯슨에서 콤마의 USB-NCM 인터페이스를 확인한 뒤 정차 상태에서 임시로 `192.168.100.250/24`를 그 **USB 인터페이스에만** 설정합니다. 다른 유선 LAN·Wi-Fi에 설정하면 안 됩니다. 네트워크 지속 설정은 검증 후에 따로 진행합니다.
4. 콤마4에서도 `usb0`에 `192.168.100.127/24`가 설정되어 있고 서로 핑/ARP가 정상인지 확인합니다. 두 장치 모두 재부팅하면 임시 주소가 사라질 수 있습니다.
5. Jetson의 검증한 cdc_ncm interface를 `NEXO_JETLINK_USB_IFACE`, 콤마에서 확인한 SHA를 `NEXO_JETLINK_MODEL_SHA256`로 명시해야 합니다. SO_BINDTODEVICE 권한이 없으면 NCM 경로는 실패하며 일반 Python 바이너리에 권한을 일괄 부여하지 않습니다. 두 장치의 연결을 **정차 상태에서 검증한 뒤**, Jetson에서 새 브랜치의 설치 스크립트를 `NEXO_JETLINK_TRANSPORT=ncm-tcp`로 실행하여 서버를 TCP 모드로 설치합니다. 이 스크립트는 기존 서비스를 변경하고 재시작하므로 백업과 원격 SSH 복구 경로가 확보된 상태에서만 실행해야 합니다.
6. 콤마4를 오프로드 상태로 유지하고 `bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh enable-ncm-tcp`를 실행합니다. 이후 프로그램 재시작 시 NCM-TCP가 선택됩니다. 새 브랜치를 차량에 배포하기 전에는 절대 실행하지 마세요.
7. `cat /dev/shm/nexo-jetlink.json`에서 ready와 정확한 모델 해시, Jetson 서버 수신, 실제 HUD USB 전송 및 재연결 시험을 **각각** 확인합니다. 단순 `ping`이나 `configured`는 추론 준비를 입증하지 않습니다.

### 복구

- 오프로드 상태에서 `bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh disable`로 실험 기능을 비활성화합니다. 기존 콤마 내부 모델을 우선 유지합니다.
- 이 방법은 AGNOS `g1`, ADB, 기존 NCM을 강제로 해제하거나 삭제하지 않습니다.
- 별도의 USB bulk 연결은 `enable`을 통해 선택할 수 있지만 `g1` 충돌을 해결하지 않은 상태에서는 성공하지 않습니다.
- **실제 차량 주행용으로 활성화하지 마세요.** USB-NCM TCP 경로의 지속 연결, 유효한 엔진 출력, 추론 지연, 모델전환 안전, HUD/YOLO 동시 사용, 방화벽/피어 인증은 아직 실기에서 확인되지 않았습니다. 특히 TCP는 인증되지 않은 전송이므로 같은 네트워크의 접근 통제가 필요합니다.

## 절대 변경하지 않는 부분

CAN, Panda, 현대 SCC/FCA, MED, 크루즈, 레이더, 정지선, R/D 처리, 운전자 감시, 기존 HUD 화면 레이아웃, YOLO 및 실차 조작 기능은 변경하지 않았습니다.

## 2026-10-20 수정 및 검증 결과

전체 근거·실제 오류·파일별 수정·미검증 사항은 [정밀 검토 보고서](../../../../docs/jetlink_ncm_review_20261020.md)에 기록했습니다.

- 실제 차량 SHA와 같은 ONNX의 pad=(-2, None) 때문에 출력 크기 계산이 실패하던 오류를 수정했습니다.
- client/server socket을 USB interface에 고정하고 직접 route/주소/실제 NCM을 검증합니다.
- 메시지 전체 송신 기한, 동시 read-ahead의 timeout 간섭, 유휴/부분 메시지 및 malformed TCP drain을 보완했습니다.
- NCM server에서 upload/shutdown/Wi-Fi provisioning 및 다른 모델 요청을 차단합니다.
- IP/source 제한은 암호학적 인증이 아닙니다. 알려진 전용 케이블에서만 정차 검증해야 합니다.
- 기존 150 ms 추론 기한과 active 실패 시 modeld restart 정책은 유지합니다. 무중단 로컬 복귀를 보장하지 않습니다.
- 장치에 설정을 적용하지 않았으며 주소 지속성, Linux interface 권한, TensorRT 성능과 HUD/YOLO 동시 실물 동작은 미검증입니다.
