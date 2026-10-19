# NEXO Jetlink USB 충돌 검토 · 2026-10-19

이 문서는 Carrot 코드 검토 및 사용자가 제공한 차량 진단 결과를 구분합니다. 실차 USB 전환, 젯슨 TensorRT 추론, HUD 출력 성공을 주장하지 않습니다.

## Carrot 벤치마킹

- 기준: ajouatom/openpilot, carrot-wip 커밋 ba186915179816a9b0476cec52bf321e73d2d4b0 (확인 당시 최신).
- 근거 코드: tools/jetlink/setup_gadget.sh, openpilot/selfdrive/modeld/jetlink/daemon.py.
- 당근도 콤마가 USB *device* (FunctionFS bulk), 젯슨이 USB *host*입니다. Jetson의 USB-A 호스트 연결이 실물 검증된 경로이고 C-to-C에는 별도의 역할 정책/핫픽스가 있습니다.
- 당근의 setup_gadget.sh 역시 다른 USB gadget이 UDC를 점유하면 실패합니다. 코드를 가져오기만 해서는 AGNOS g1 (ADB/NCM) 점유 문제가 없어지지 않습니다.
- 당근의 Cinque 모델이나 전용 설정을 NEXO에 복사하지 않습니다. 기존 NEXO driving_supercombo.onnx 및 SHA/입출력 계약을 유지합니다.

## 차량에서 확인된 내용

- USB device UDC: a600000.dwc3 한 개.
- 점유: g1 (ffs.adb + ncm.0).
- UDC state=configured: g1이 USB 호스트에 의해 설정된 것으로, Jetlink 추론 성공을 뜻하지 않습니다.
- usb0는 물리적 연결과 RX/TX 기록이 있지만 Jetlink protocol 연결을 증명하지 않습니다.
- 과거 IP 192.168.100.250으로의 기본 경로는 wlan0였습니다. 사용자가 임시로 usb0에 192.168.100.127/24를 더했으나 상대 IPv4 응답은 없었습니다.
- IPv6 멀티캐스트에서 fe80::6b15:550e:2406:ab9a%usb0가 한 차례 응답했고 공개키 SSH 인증 오류를 남겼습니다. 이후 통신은 실패했습니다. 이 장치가 실제 젯슨인지, SSH 서버가 지속 실행 중인지까지는 확인되지 않았습니다.
- NEXO Jetlink 로그: USB gadget 'g1' already owns a600000.dwc3; 외부 추론 active=false.

## 반영한 안전한 개선

1. usb_preflight.py: configfs UDC, 점유 gadget의 이름과 기능, usb0의 연결 상태를 읽기 전용으로 보여줍니다.
2. 7000번대의 nexo_jetlink_diag.py, nexo_jetlink_ctl.sh status에 이 결과를 표시합니다.
3. daemon.py: 이미 점유한 gadget 문제를 단순 retrying이 아닌 conflict 상태로 분리합니다.
4. 가상 sysfs 회귀 시험 추가: g1 ADB/NCM, 정상 Jetlink, 다른 점유자, UDC 부재, usb0 부재를 구분합니다.

이 변경만으로 젯슨의 추론이 활성화되는 것은 아닙니다. 현재 방식은 별도의 Jetlink FunctionFS gadget이 필요하며 기존 g1과 같은 UDC에 동시에 바인딩할 수 없습니다.

## 추후 정차 상태에서 해결해야 할 작업

- 젯슨 전원/부팅 및 USB-A 호스트 인식, nexo-jetlink-server 설치와 모델 캐시를 먼저 검증합니다.
- 콤마에서 기본 ADB/NCM 접속이 끊어질 수 있으므로 와이파이 SSH 또는 콘솔의 **별도 복구 경로를 확보한 상태**에서만 USB g1 → Jetlink 배타적 전환 방식과 복귀 절차를 설계해야 합니다.
- Jetlink HELLO, NEXO 모델 SHA, TensorRT 준비, HUD 데이터, YOLO 서비스, 링크 오류 시 로컬 모델 복귀를 각 단계별로 실물 검증합니다.
- 기존 CAN/Panda/SCC/MED/레이더/운전자 감시 및 모델 계약은 변경하지 않습니다.

**주의:** 현재 구현은 AGNOS g1/UDC를 강제로 해제하거나 ADB를 끄지 않습니다. g1/UDC에 빈 문자열을 쓰면 원격 접속이 끊길 수 있으므로 실행하지 마십시오.

## 콤마의 확인 명령 (코드를 실제 설치한 뒤)

    cd /data/openpilot
    python3 openpilot/selfdrive/modeld/jetlink/usb_preflight.py
    bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh status

참고: Carrot 문서 docs/jetlink_experiment.md 및 tools/jetlink/USB-C-HOTFIX.md. Jetlink 서버와 모델/HUD 정상 작동 여부는 이 진단만으로 증명할 수 없습니다.
