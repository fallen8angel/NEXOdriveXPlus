# comma4 USB-NCM 부팅·재연결 복구

## 2e03c42 이후: 주소 없는 AGNOS 인터페이스 복구 수정

이번 수정의 기준은 `2e03c4242d941115ce8490e8f55590502c586613`이다.
사용자는 worker 실행, `comma_interface() == usb0`, sudo 권한을 확인했지만,
재부팅 뒤 `ip -j -4 addr show dev usb0`가 `[]`를 반환한다고 보고했다.

수정 전에 이 응답을 대역으로 주고 실제 함수를 실행한 결과:

- 호출 경로는 `main → ensure_ncm_network → addresses`였다.
- 반환값은 `('error', 'NCM address inspection no longer matches the discovered interface')`였다.
- 링크 UP, 주소 추가, 호스트 경로 추가 명령은 하나도 실행되지 않았다.

기존 코드는 IPv4 조회에 항상 인터페이스 행 하나가 있을 것으로 가정했다.
주소가 없는 정상 인터페이스를 장치 불일치로 잘못 판단하여 복구를 막는 코드 결함을
확인한 것이다. 장비에서 오류 로그를 직접 수집한 것은 아니며, 어떤 OS 구성 요소가
주소를 지우는지까지 확인한 것은 아니다.

수정 후에는 `ip -j link show dev <NCM>`으로 ifname/ifindex/관리 UP 상태를 별도로
검증한다. 성공한 IPv4 조회의 `[]`는 주소 없음으로 받아들이고, 필요할 때 링크 UP,
`.127/24 noprefixroute`, Jetson `.250/32` 경로를 순서대로 복구한다.
조회 실패·잘못된 JSON·다른 장치의 행은 여전히 오류다.
빈 응답 전후에도 configfs NCM/ifindex를 재확인하여 재열거 중 잘못된 장치에 쓰지 않는다.
오류 로그에는 인터페이스, ifindex, 실패 단계/변경 명령, 예외와 stderr를 남긴다.

이번 변경 파일은 `network.py`, `test_jetlink_network_recovery.py`, 이 문서뿐이다.
manager의 기존 2초 반복 및 추론 OFF 실행 조건은 그대로 재사용한다.
ONNX, Jetson 설치기/서비스, HUD, 차량 제어 파일과 모델은 변경하지 않았다.

이번 검증 결과:

- 집중 테스트 **57 passed**. 새 회귀 17개를 추가했으며 기존 fixture도 주소 없을 때
  기본적으로 실제 보고된 `[]`를 반환하도록 바꿨다.
- 최종 전체 회귀 **469 passed, 4 skipped, 0 failed**. 기존 ONNX 실제 모델 검사,
  설치 자동 복구, TCP/HUD 및 manager 검사 포함. Ruff·Python 3.10 문법·whitespace 검사 통과.
- 첫 전체 실행은 **468 passed, 1 failed, 4 skipped**였다. 기존 보호형 설치 테스트의
  Windows `os.rename()`에서 `WinError 5`가 발생했다. 설치기만 재실행했을 때 원래
  실패한 테스트는 통과했지만 다른 테스트에서 같은 rename 오류가 발생했다.
  코드나 테스트를 변경하지 않고 새 OS 임시 디렉터리에서 전체 재실행하여 모두 통과했다.
  Windows 오류를 일으킨 외부 원인까지 확정하지는 않았다.
- skip 4개와 기존 경고 19개는 아래 이전 검증 기록과 동일하다.
- 재부팅/양쪽 동시 부팅/USB 탈착/지연/이름 변경/추론 OFF/Wi-Fi 보존은 시뮬레이션이다.
  실제 AGNOS의 새 link 조회, sudo, 양방향 ping, ADB 유지, 장시간 재연결은 **실기 미검증**이다.

현재 Jetson NCM 설치가 롤백됐다는 사용자 보고가 있으므로, 양쪽 영구 복구가 완료됐다고
판단할 수 없다. 아래 Jetson 추가 설치 항목의 기존 패키지 복구 조건이 여전히 필요하다.
이번에는 장비에 접속하거나 설치/설정을 변경하지 않았고 ZIP도 다시 만들지 않았다.
GitHub 반영만으로 실제 comma4가 업데이트되는 것은 아니다.

아래는 2e03c42까지의 이전 구현·검증 및 적용 참고 기록이다.

## 원인과 누락

기준 NEXO 커밋: `5cceb9de80bf6e358eaeac7c0456ecf29c7227d7`.

사용자가 확인한 사실은 comma4 재부팅 후 `usb0`의 IPv4가 사라지고,
`sudo -n ip addr add 192.168.100.127/24 dev usb0`로 다시 연결된다는 것이다.
이 명령은 현재 커널 네트워크 장치에 주소를 넣는다. 장치 재생성/시스템 재부팅 뒤에
자동 재적용되는 영구 설정이 아니다.

코드에서 확인한 누락은 다음과 같다.

1. 기존 `network.py` 작업자는 `ip link set ... up`만 수행하며 주소·경로를 설정하지 않았다.
2. manager 실행 조건은 `offroad_link_allowed`였으며 Jetlink 활성 marker가 있어야 했다.
   따라서 추론 OFF에서는 네트워크 작업자도 실행되지 않았다.
3. `ncm_transport.py`는 주소·carrier·직접 경로를 검사하는 읽기 전용 코드이므로
   누락된 주소를 복구하지 않는다. Jetson의 기존 `protected_ncm.py`도 상대 comma의 IP를 설정할 수 없다.

이 작업에서는 실제 부팅 로그나 comma NetworkManager 동작을 관측하지 않았다.
특정 OS 서비스가 주소를 지웠다고 단정하지 않으며, 확인된 임시 주소와 재적용 누락을 해결한다.

## 구현

기존 `nexo_jetlink_network` PythonProcess를 그대로 재사용한다.
TICI 장치에서 manager가 시작하면 항상 실행되고, 작업자 crash 시 기존 manager가 재시작한다.
IsOnroad/IsOffroad, `NexoJetsonUsb`, Jetlink enable marker/transport 파일에 의존하지 않는다.
네트워크 설정만 수행하므로 차량 추론 활성화나 로컬 모델 선택에 영향을 주지 않는다.

2초마다 `ensure_ncm_network()`가 다음을 수행한다.

- 기존 `ncm_transport.comma_interface()`를 사용하여 UDC가 연결된 configfs NCM 함수의
  실제 ifname을 찾는다. `usb0`라는 이름만 보고 장치를 선택하지 않는다.
- 하나의 실제 활성 NCM 장치만 허용한다. 없는 경우 늦은 USB 열거를 기다리고,
  여러 장치가 있거나 NCM이 아니면 설정하지 않는다.
- 필요할 때만 링크를 UP으로 바꾸고 `192.168.100.127/24`를 추가한다.
- 새 주소에 `noprefixroute`를 사용해 USB `/24` 연결 경로가 자동 생성되지 않게 한다.
  추가 경로는 Jetson `192.168.100.250/32 dev <NCM> src 192.168.100.127 proto 186` 한 개뿐이다.
- 이미 존재하는 IP/경로는 중복 추가하지 않는다. 주소 소실·관리 DOWN·경로 소실은 다음 회차에 복구한다.
- 각 변경 직전과 직후 NCM ifname/ifindex를 다시 확인한다. 재열거되면 다음 회차에 재탐색한다.
- 상대 장치의 IP나 USB carrier가 아직 없어도 먼저 자신의 주소를 설정한다.
  carrier 연결 후 기존 `require_usb_route()`로 실제 직접 경로를 확인한다.

Wi-Fi·기본 경로·DNS·NetworkManager 설정·g1·ADB·UDC·USB role은 변경하지 않는다.
다른 장치로 향하는 기존 `.250/32` 경로나 잘못된 기존 주소 prefix는 덮어쓰지 않고 오류를 기록한다.
기존 수동 설정이 만든 `/24` 경로는 그 설정의 소유자가 관리하므로 이번 작업자가 삭제하지 않는다.
`ip` 변경은 root 또는 `sudo -n`으로 실행하고 timeout/권한 오류는 기록한 뒤 재시도한다.
worker 로그 상태는 `waiting_ncm`, `waiting_usb`, `ready`, `error`다.
`ready`는 USB 주소·경로·carrier 확인이며 ping이나 AI 추론 성공을 뜻하지 않는다.

이전 명시적 offroad 링크 전용 helper와 읽기 전용 상태 API는 호환성을 위해 유지했다.
Jetson의 보호형 네트워크 복구 구현과 설치기는 수정하지 않았다.

## 수정 파일

| 파일 | 변경 |
|---|---|
| `openpilot/selfdrive/modeld/jetlink/network.py` | 기존 worker에서 NCM 주소·호스트 경로 주기적 복구 |
| `openpilot/system/manager/process_config.py` | 기존 TICI network worker를 추론과 독립적으로 실행 |
| `openpilot/selfdrive/modeld/tests/test_jetlink_network.py` | 변경된 manager 실행 조건 검사 |
| `openpilot/selfdrive/modeld/tests/test_jetlink_network_recovery.py` | 부팅/탈착/지연/중복/경로/권한/재열거 회귀 |
| 이 문서 | 원인·적용·검사 범위 |

ONNX 변환기, 모델, 보호형 installer/probe, Jetson NCM worker, Carrot 서비스,
외부 HUD, MED/SCC/레이더/CAN/DBC/차량 제어 코드는 변경하지 않았다.

## 검증 결과

전체 관련 회귀: **452 passed, 4 skipped, 0 failed, 0 deselected**.
기존 ONNX 실제 모델 수치 검사, 설치 자동 복구, NCM TCP, HUD, manager 생성자 검사 포함.
Ruff, Python 3.10 문법 및 git whitespace 검사 통과.
skip은 실제 pyray 1개, Linux cereal/msgq 2개, 실제 Linux flock 1개다.
기존 pytest 설정/aiohttp 경고 19개가 남아 있다.
정적 검사 중 테스트 fixture import 이름의 F811 경고를 수정한 뒤 Ruff를 통과했다.

새 회귀는 실제 configfs 감지·ip JSON 해석·경로 검사 코드를 실행하며
sysfs 파일, 커널 네트워크 상태와 `ip`/`nmcli` 변경은 명시적 대역으로 구성했다.
양쪽 시험은 기존 Jetson `protected_ncm.network_once()`를 수정 없이 직접 호출한다.

| 요청 상황 | 자동화 검사 | 실제 장비 |
|---|---|---|
| comma4 단독 재부팅 | 커널 주소/경로 소실·새 ifindex 복구 통과 | 미검증 |
| Jetson 단독 재부팅 | 기존 Jetson worker의 IP 복구와 comma 대기/재연결 통과 | 미검증 |
| 양쪽 동시 재부팅 | 양쪽 carrier DOWN에서 선제 주소 설정 후 복구 통과 | 미검증 |
| USB 분리·재연결 | 장치 소실·usb1 재열거·ifindex 변경 통과 | 미검증 |
| Jetlink 비활성화 | marker/transport 조회 없이 설정, manager gate 통과 | 미검증 |
| USB 인식 지연 | 실제 worker 반복 루프 대기 후 복구 통과 | 미검증 |
| 기존 IP 존재 | 중복 주소 추가 없음, 누락 경로만 설정 통과 | 미검증 |
| Wi-Fi 연결 | 기본/Wi-Fi subnet 경로 보존, USB `/32`만 추가 통과 | 미검증 |

이번 작업에서 장치 접속/설치/재부팅은 실행하지 않았다.
실제 AGNOS 권한·iproute2·configfs, NetworkManager와의 공존, g1/ADB 유지,
실제 양방향 ping 및 장시간 재연결은 현장에서 확인해야 한다.
실제 GPU TensorRT 검증 상태도 이전과 같으며 이번 수정으로 검증됐다고 주장하지 않는다.

## comma4 적용 방법

정차·오프로드에서 comma4에 SSH 접속한다.

```sh
cd /data/openpilot
git status --short
git branch --show-current
git remote get-url origin
```

현재 브랜치가 NEXO이고 origin이 `fallen8angel/NEXOdriveXPlus`인지 확인한다.
로컬 수정이 있으면 보존·비교 후 반영하고, `reset --hard`나 강제 checkout으로 지우지 않는다.
작업 트리가 깨끗하고 브랜치/remote가 맞으면:

```sh
git pull --ff-only origin NEXO && sudo -n reboot
```

업데이트 실패 시 `&&` 뒤의 reboot는 실행되지 않는다. fast-forward 불가 시
로컬 커밋을 보존한 상태에서 차이를 확인해야 한다.
재부팅 후 openpilot manager가 시작하면 network worker가 자동 실행된다.
따로 IP를 추가하거나 Jetlink inference를 enable할 필요는 없다.

comma4에서 읽기 전용 확인:

```sh
pgrep -af nexo_jetlink_network
ip -br -4 addr
ip -4 route get 192.168.100.250 from 192.168.100.127
ip -4 route show default
ping -c 5 192.168.100.250
test ! -e /data/nexo_jetlink_enabled && echo "Jetlink inference OFF"
cat /sys/kernel/config/usb_gadget/g1/UDC
```

프로세스 표시 이름은 실행 환경에 따라 module 경로로 보일 수 있다. 주소/경로와 manager
로그의 `NEXO USB-NCM network` 상태를 함께 확인한다.
추론 marker가 이미 존재했다면 이번 코드가 이를 자동 삭제하거나 생성하지 않는다.
기존 offroad disable 절차로 OFF 상태를 만든 뒤 시험한다.

## Jetson 추가 설치 여부

**이전에 제공한 NCM 보호형 패키지가 설치되어 있다면 추가 설치가 필요 없다.**
기존 `NEXO_Jetson_protected_NCM_ONNX_20261021.zip`을 그대로 재사용한다.
새 ZIP은 만들지 않는다. 이번 변경은 comma manager/network 파일에만 필요하다.

Jetson에서 다음 상태를 확인한다.

```sh
cat /opt/carrot-jetlink/updates/nexo-ncm.json
systemctl status nexo-jetson-ncm.service --no-pager -l
cat /run/nexo-ncm-status.json
ip -br -4 addr
ping -c 5 192.168.100.127
```

기존 DATA NCM 설정과 boot updater가 유지돼 있어야 Jetson 재부팅 후 `.250/24`가 복구된다.
이전 설치가 실패하여 Carrot으로 rollback됐거나 NCM 설정이 없다면 이 comma 수정만으로
Jetson의 영구 복구가 생기지는 않는다. 기존 ZIP의 설치/복구 안내로 먼저 그 상태를 해결한다.
새 설치기나 ONNX 모델을 다시 만들 필요는 없다.
