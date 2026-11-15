# 콤마4 LTE → 젯슨 USB-NCM 인터넷 공유 (수동 시험용)

후속 구현·벤치마킹·검증 범위는 [2026-11-15 후속 보고서](jetson_followup_20261115.md)를 참조한다.

## 현재 상태와 목적

콤마4 LTE 회선과 젯슨 사이의 별도 USB-NCM 통신(`192.168.100.127 ↔ 192.168.100.250`)이
가능하더라도 젯슨이 인터넷을 사용할 수 있다는 뜻은 아닙니다. 기본 경로, 포워딩, NAT, DNS가 필요합니다.

기존 `jetson_hud_updater.py`는 콤마4가 릴리스 파일을 다운로드하고 SSH/SCP로 젯슨에 전송하도록
설계되어 있습니다. 젯슨의 직접 인터넷 연결 없이도 가능하지만, 릴리스 누락/SSH/보호형 사전 검사
실패는 별도 원인으로 남아 있습니다.

신규 `openpilot/tools/jetson/lte_share.py`는 **젯슨 자체에서 인터넷을 쓰는 경우**를 위한 선택 기능입니다.
**기본 비활성**이고 부팅 시 자동 실행되지 않습니다. 실차의 NAT 도구, 실제 LTE 인터페이스,
DNS 및 패키지 업데이트 여부는 검증되지 않았습니다.

## 실행 전 조건

- 콤마4와 젯슨이 USB-NCM으로 연결되고 콤마4에서 `jetlink@192.168.100.250`으로
  배치 SSH 및 젯슨 `sudo -n` 명령이 가능해야 합니다.
- 콤마4의 현재 인터넷 경로가 셀룰러 장치(`rmnet*`/`wwan*`/`ccmni*`/`pdp*`) 또는
  실제 활성 point-to-point PPP 링크로 확인된 콤마4 `ppp0`여야 합니다.
  Wi-Fi/이더넷/VPN 및 확인되지 않은 인터페이스는 거절합니다.
- 콤마4에서 `sudo -n iptables` 또는 `sudo -n iptables-legacy`의 NAT/FORWARD 검사,
  그리고 `sudo -n sysctl`이 지원되어야 합니다. 선택한 방화벽 도구는 저장하여 같은 도구로 원복합니다.
- 콤마4가 `IsOffroad=1` 및 `IsOnroad=0`인 완전 오프로드여야 합니다.
  P단이라도 Onroad 상태면 변경하지 않습니다.
- 통신량만큼 LTE 데이터 요금이 발생할 수 있습니다.

## 콤마4에서 최초 시험

완전히 정차해 시동을 끄고 콤마4 SSH 접속 후 실행합니다.

~~~sh
cd /data/openpilot
PYTHONPATH=/data/openpilot python3 -m openpilot.tools.jetson.lte_share status
PYTHONPATH=/data/openpilot python3 -m openpilot.tools.jetson.lte_share enable
PYTHONPATH=/data/openpilot python3 -m openpilot.tools.jetson.lte_share status
~~~

`enabled: true`는 포워딩, 자체 방화벽 3개 규칙, 젯슨 USB 기본 경로가 확인되었다는 뜻입니다.
실제 LTE 데이터 전송이나 DNS 성공까지 증명하지는 않습니다. 완전 Offroad에서 명시적으로 검사합니다:

~~~sh
PYTHONPATH=/data/openpilot python3 -m openpilot.tools.jetson.lte_share probe
~~~

기존 Wi-Fi 기본 경로는 변경하지 않고 USB 기본 경로만 `metric 1900`으로 추가합니다.
Wi-Fi는 연결됐지만 인터넷만 장애인 경우 자동 우회를 보장하지 않습니다.
검사는 USB 인터페이스·source IP에 묶은 DNS와 인증된 HTTPS를 실행합니다. 시스템 resolver는
별도 검사하며 Wi-Fi 성공을 LTE 성공으로 인정하지 않습니다. 결과는 120초·동일 부팅/구성에서만
유효합니다. DNS 실패 시에도 네임서버를 강제로 수정하지 않습니다. `probe`는 기존 Jetson sudo 권한을 사용합니다.

원복:

~~~sh
PYTHONPATH=/data/openpilot python3 -m openpilot.tools.jetson.lte_share disable
~~~

SSH가 끊겨 원복 확인이 실패하면 USB가 복구된 이후 `disable`을 다시 실행해야 합니다.
재부팅 후 자동 활성화되지 않으므로 상태를 다시 확인하십시오.

## 보호하는 기능과 한계

NCM의 g1/ADB/UDC, USB 주소, 콤마4 기본 경로, Wi-Fi 프로필, HUD 패널 및 AI 추론,
CAN/차량 제어 코드를 변경하지 않습니다. 콤마4에는 젯슨 **단일 IP**로 제한된 NAT/포워딩만 추가합니다.
젯슨에는 기존 기본 경로를 삭제하지 않고 고유 프로토콜(187)의 후순위 경로만 추가합니다.

GitHub 코드 변경은 콤마4 실장 적용과 별개입니다.
7000 서버 젯슨 관리 화면에 상태 확인·공유 켜기·공유 끄기 버튼을 추가했습니다.
다만 상시 자동 재연결 기능은 아직 구현하지 않았습니다.
실차 검증 전에 부팅 시 네트워크를 자동 변경하도록 확장하지 마십시오.
