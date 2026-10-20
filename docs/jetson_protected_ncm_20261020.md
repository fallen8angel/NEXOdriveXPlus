# 보호형 Jetson USB-NCM 설치·복구 — 2026-10-20

## 작업 기준과 확인 범위

| 저장소 | 시작 시 최신 HEAD |
|---|---|
| NEXOdriveXPlus / NEXO | `6e6a3f8663c72dbd084e11ebdd8a3d9d697fdf8a` |
| ajouatom/openpilot / carrot-wip | `10cd6868d3715d7068bd38634bf7b808ba9d1834` |
| ajouatom/carrot-jetson | `d0dc96fa32410403dfb2fefff3e2909c11ec109b` |

GitHub 최신 HEAD와 Jetson feature branch 목록을 다시 확인했다. 기존 PR #28/#29 및
`codex/jetson-recovery-20261020`의 NCM 전송·재열거/상태 복구는 이미 메인에 들어 있다.
새 브랜치 `codex/jetson-protected-ncm-20261020`에서 보호형 설치와 네트워크 지속성만 보완한다.
당근의 최신 BSD/vision 수명 관리 변경은 이번 문제와 범위가 달라 가져오지 않았다.
보호형 저장소 구성·서비스 이름·부팅 updater 순서는 고정 carrot-jetson 소스를 직접 읽어 재사용했다.

사용자가 실기에서 확인한 사실은 comma `.127/24` ↔ Jetson `.250/24` 양방향 ping 무손실,
comma `usb0`, Jetson `enx36770f430ea2`, NCM USB `04d8:1234`, TURZX `1cbe:0092`다.
이 작업에서 장치에 접속하거나 설치하지 않았다. 이 ping 결과를 모델 통신 성공으로 확대 해석하지 않는다.

## 원인과 수정 방향

g1이 정상 점유 중이고 Jetson 서버는 `--transport usb`로 `1209:0001`을 찾는다.
실제 연결은 `04d8:1234`의 USB-NCM이므로 기존 TCP adapter를 명시적으로 선택해야 한다.
또한 NetworkManager가 관리하는 interface의 임시 IPv4가 사라지므로 주소를 수동으로 한 번 넣는 것으로는 부족하다.

기존 standalone 설치기는 `/etc/systemd`를 변경하므로 보호형 APP에는 적합하지 않았다.
이제 해당 스크립트는 보호형 marker를 발견하면 변경 전에 거부한다.
기존 DATA-only `protected_install.py`, 배포 bundle builder, 고정 server/native HUD launcher를 재사용한다.
g1/ADB/USB role·전원·VID/PID를 변경하거나 별도의 USB gadget을 생성하지 않는다.

## 구현 내용

- DATA `/opt/carrot-jetlink/updates/nexo-ncm.json`에 명시적 interface·NEXO SHA·transport를 저장한다.
  루트 APP나 SETUP에 서비스/NetworkManager 파일을 쓰지 않는다.
- 기존 boot updater bridge가 선택된 NEXO release를 확인한 뒤 **boot activate에서만**
  `/run/systemd/system`의 server drop-in과 root network worker를 재생성한다.
  일반 자동 업데이트 timer는 전송 방식을 다시 적용하거나 서비스를 재시작하지 않는다.
- `carrot-jetlink.service`와 `carrot-jetlink-hud.service`를 그대로 재사용한다. server만
  `.250:5599`, `--transport tcp`, 동일 cache/control socket, 고정 모델 SHA 환경으로 바꾼다.
  서버 서비스에만 CAP_NET_RAW를 부여한다. 일반 Python 바이너리에 setcap하지 않는다.
- 기존 HUD unit의 DISPLAY/XAUTHORITY/권한/레이아웃과 YOLO 실행 경로는 유지한다.
  별도 nexo/jetlink server·HUD unit이 이미 active면 설치를 거부하며 임의로 종료하지 않는다.
- network worker는 `cdc_ncm`이고 USB ancestor가 `04d8:1234`인 장치에만 동작한다.
  설치 이름이 사라졌을 때는 이 조건의 유일한 NIC만 다시 찾는다. TURZX·Wi-Fi·다른 Ethernet은 거부한다.
- 해당 USB NIC만 NetworkManager의 런타임 관리에서 제외하고 admin-UP 및 `.250/24`를 복구한다.
  peer `.127/32` 전용 route만 필요 시 추가하며 기존 충돌 host route는 덮어쓰지 않는다.
  default route·DNS·LAN/Wi-Fi profile을 변경하지 않는다.
- 기존 client/server의 주소·직접 route·carrier·SO_BINDTODEVICE 검사, source allowlist,
  HELLO/모델 계약/warmup 및 재연결을 유지한다. 네트워크 복구 loop는 2초 간격이며 inference/HUD 서비스를 restart하지 않는다.
- 네트워크/소유권 lease와 오류 상태는 RAM `/run/nexo-ncm-{leases,status}.json`에 기록한다.
  DATA에는 수동 설치/복구 시에만 설정·트랜잭션을 저장한다.
- 설치는 실제 USB network readiness와 해당 서비스 MainPID가 `.250:5599`를 소유하는지 확인한다.
  `ss`의 `%interface`/`%ifindex` 표기도 처리하고 서버의 늦은 시작을 제한된 시간 동안 기다린다.

NetworkManager의 device managed 설정은 런타임 제어이므로 DATA의 boot hook이 매 부팅 재적용한다.
근거: [NetworkManager nmcli](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html),
[systemd service 권한](https://www.freedesktop.org/software/systemd/man/250/systemd.exec.html).

## 모델과 TensorRT 검증

현재 NEXO의 실제 ONNX SHA는 다음과 같으며 변경하지 않았다.

`f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`

60,792,584 bytes, frame_skip=4, 영상 393,216 bytes, context float32 524개,
출력 `[1,2576]` float32 계약을 기존 pinned parser/실제 ONNX 회귀에서 다시 확인한다.
사용자가 보고한 당근 SHA `09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec`는
다른 모델이며 이를 NEXO 모델로 허용하지 않는다.

bundle/installer/helper SHA, source commit, runtime ABI, 전체 ONNX metadata 및 사용자에게 받은 comma SHA를 대조한다.
설치 후보는 기존 cache에 같은 NEXO 모델을 준비하고 TensorRT engine의 전체 spec과
실제 출력 key/shape/dtype/finite를 3회 검사한 뒤에만 current release를 바꾼다.
기존 Carrot 모델·엔진을 삭제하지 않고 후보 probe의 cache pruning도 기존처럼 억제한다.
Python 3.10 / L4T 36.4.7 / TensorRT 10.3.0 이외 환경은 자동 변경하지 않고 거부한다.

이는 실제 설치 때 수행할 검증 코드다. 이 개발 환경에서 실제 GPU 엔진을 빌드하거나 정확도·지연을 측정한 것은 아니다.
온로드 활성화 조건·150ms 추론 deadline·active 추론 실패 후 기존 modeld restart 정책은 변경하지 않았다.
차량 inference marker를 생성하지 않으므로 GitHub 코드 반영만으로 기능이 활성화되지 않는다.

## 설치 실패·중단과 복구

설치 전 previous release, 원래 last-loaded 모델 metadata, 이전 NCM 설정을 DATA 트랜잭션에 보존한다.
GPU/네트워크/listener/service 확인 실패 시 기존 소유자를 멈추고 원래 release/model/NCM 설정을 복구한다.
root network worker는 자신이 추가한 주소·전용 route만 제거하고 관측한 이전 NM 관리/admin 상태를 복원한다.
원래부터 있던 수동 USB 주소는 삭제하지 않는다. 다른 프로세스가 바꾼 route를 강제로 지우지 않는다.
USB가 제거돼 ifindex가 달라진 lease에는 작업하지 않는다.

중단된 transaction은 다음 boot activate에서 기존 services 시작 전에 복구한다.
복구가 실패하면 transaction을 남기고 `rollback incomplete`로 실패 처리한다.
current를 바꿨다는 이유만으로 설치 성공이나 차량 통신 성공을 표시하지 않는다.
설치 성공 뒤 수동 `rollback --confirmed-offroad`도 제공한다.
기존 Carrot updater는 checksum과 함께 DATA에 보존하고, Carrot을 다시 선택하면 원래 updater로 위임한다.

## 실제 설치 명령 — 직접 실행하기 전 읽기 전용 확인

이 문서의 설치는 **정차·오프로드에서 사용자가 명시적으로 실행**하는 절차다.
이 작업에서는 SSH 접속·파일 전송·장치 설치·서비스 변경을 실행하지 않았다.
comma의 로컬 변경 41개 보존/비교는 기존 [실기 확인 절차](Jetson_실기확인_20261020.md)의 백업 순서를 따른다.

Jetson에 LAN SSH로 접속한 터미널에서:

```sh
findmnt -T / -o TARGET,SOURCE,FSTYPE,OPTIONS
findmnt -T /opt/carrot-jetlink -o TARGET,SOURCE,FSTYPE,OPTIONS
cat /run/carrot-storage.json
df -h /opt/carrot-jetlink
systemctl cat carrot-jetlink.service carrot-jetlink-hud.service carrot-jetlink-update-apply.service
/opt/carrot-jetlink/venv/bin/python -c 'import sys,tensorrt; print(sys.version); print(tensorrt.__version__)'
ip -br -4 addr
lsusb -t
```

예상: APP p1 read-only, DATA p17의 `/runtime`이 `/opt/carrot-jetlink`로 rw bind mount,
storage `protected`, Python3.10/TRT10.3.0, 기존 Carrot unit의 `/current/tools/jetlink` 경로다.
다르면 remount/강제 업데이트 대신 실제 구성을 확인해야 한다.

제공한 PC용 ZIP을 풀면 tar.gz, JSON manifest, installer와 helper가 들어 있다.
아래처럼 DATA의 수신 폴더를 준비하고 PC에서 파일을 복사한다.

```sh
# Jetson 터미널, 읽기 전용 확인을 마친 뒤:
sudo install -d -o jetlink -g "$(id -gn jetlink)" -m 0750 /opt/carrot-jetlink/updates/nexo-incoming
```

```powershell
# Windows PC: ZIP을 푼 폴더에서, 실제 LAN SSH 주소를 사용:
scp NEXO-protected-ncm.tar.gz NEXO-protected-ncm.json protected_install.py protected_ncm.py jetlink@192.168.45.196:/opt/carrot-jetlink/updates/nexo-incoming/
```

Jetson에서 package manifest와 함께 제공한 SHA256SUMS를 대조한 뒤:

```sh
cd /opt/carrot-jetlink/updates/nexo-incoming
sha256sum NEXO-protected-ncm.tar.gz protected_install.py protected_ncm.py
BUNDLE_SHA=$(/usr/bin/python3 -c 'import json;print(json.load(open("NEXO-protected-ncm.json"))["bundle_sha256"])')
sudo /usr/bin/python3 protected_install.py install \
  --bundle "$PWD/NEXO-protected-ncm.tar.gz" --sha256 "$BUNDLE_SHA" \
  --ncm-interface enx36770f430ea2 \
  --model-sha256 f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5 \
  --confirmed-offroad
```

GPU 준비에 시간이 걸릴 수 있다. 성공 기준은 `NEXO_INSTALL_OK`, DATA status `installed`,
`network_listener_verified=true`다. 이 값도 comma에서 HELLO/실제 추론을 검증했다는 뜻은 아니다.
서비스 설치 과정은 기존 Carrot server/HUD를 정지·시작하지만 YOLO를 종료하거나 새 서비스를 중복 기동하지 않는다.

```sh
cat /opt/carrot-jetlink/updates/nexo-install-status.json
cat /run/nexo-ncm-status.json
systemctl cat carrot-jetlink.service
systemctl status nexo-jetson-ncm carrot-jetlink carrot-jetlink-hud --no-pager -l
ss -H -ltnp 'sport = :5599'
ip -4 route get 192.168.100.127 from 192.168.100.250
journalctl -b -u nexo-jetson-ncm -u carrot-jetlink -u carrot-jetlink-hud --no-pager -n 120
```

예상: `.250:5599` 또는 `.250%실제NIC:5599`, TCP transport, network state=ready,
route가 USB NIC를 향하고 default route는 기존 LAN/Wi-Fi로 유지된다.
NetworkManager service 자체를 중지하거나 g1을 해제할 필요가 없다.

comma 추론은 이 설치로 켜지지 않는다. 실험을 진행하기로 결정한 정차 상태에서만 기존
`nexo_jetlink_ctl.sh enable-ncm-tcp` 절차를 별도로 사용한다. 기존 marker가 있으면 먼저 offroad disable이 필요하다.
이 단계 이전에는 로컬 모델로 유지한다.

## 복구 명령과 실기 합격 기준

정상 설치 후 원래 Carrot 선택으로 되돌리려면, comma가 offroad임을 확인하고 먼저 Jetlink 실험을 disable한 뒤:

```sh
sudo /usr/bin/python3 /opt/carrot-jetlink/updater/nexo_protected.py rollback --confirmed-offroad
readlink -f /opt/carrot-jetlink/current
cat /opt/carrot-jetlink/cache/last-loaded.json
systemctl status carrot-jetlink carrot-jetlink-hud --no-pager -l
```

설치 중단으로 transaction이 남아 manual rollback이 거부되면, 오프로드에서 해당 services를 멈춘 뒤
boot recovery를 실행한다. transaction 파일을 지우거나 모델 cache를 초기화하지 않는다.

```sh
sudo systemctl stop carrot-jetlink.service carrot-jetlink-hud.service
sudo /usr/bin/python3 /opt/carrot-jetlink/updater/nexo_protected.py activate
sudo systemctl start carrot-jetlink.service carrot-jetlink-hud.service
```

복구 오류가 나면 상태 JSON과 journal을 보존하고 원인을 먼저 해결한다.
재부팅 지속성·USB 탈착/이름 변경·서버 늦은 시작/재시작·NCM/HUD/YOLO 동시 부하를 정차 상태에서 검증한다.
g1/ADB 보존, 양방향 ping, 같은 NEXO SHA의 실제 HELLO→engine→warmup→READY,
실제 TURZX 화면, 원격 추론 정확도/P50·P95·P99·최대 지연을 각각 확인해야 한다.

## 수정 파일과 소프트웨어 검증

| 파일 | 변경 |
|---|---|
| openpilot/tools/jetson/protected_ncm.py (신규) | DATA 설정/RAM unit, 식별된 USB만 NM 제외·주소 복구, 소유 lease·복원 |
| openpilot/tools/jetson/protected_install.py | 명시적 NCM 옵션, 원래 service 재사용, 후보/모델/helper 검증, boot replay·rollback |
| openpilot/tools/jetson/protected_probe.py | 실제 TensorRT 출력 key/shape/dtype/finite 검사 |
| openpilot/selfdrive/modeld/jetlink/install_jetson_server.sh | 보호형에서 일반 /etc installer 거부 |
| openpilot/selfdrive/modeld/jetlink/diagnose_usb_owner.py | 실제 Carrot/protected unit·모델·network worker 읽기 전용 진단 |
| openpilot/tools/jetson/tests/test_protected_ncm.py (신규) | NM 주소 소실·탈착/재열거·전용 NIC/route·RAM replay·출력 계약 |
| openpilot/tools/jetson/tests/test_protected_install.py | NCM 설치/실패/중단/rollback·경쟁 owner·CLI 회귀 |
| openpilot/selfdrive/modeld/jetlink/README.md | 보호형 설치 경로 안내 |
| 이 문서 | 비교·실제 설치/복구·검증 한계 |

최종 관련 회귀 **408 passed, 4 skipped, 실패 0, 제외 0**. Windows Python3.12.14.
실제 loopback TCP/UDP, 고정 Carrot Session 및 실제 NEXO ONNX 계약은 재사용해 확인했다.
보호형 저장소/NetworkManager/ip/systemd/GPU는 명시적 대역이며 실제 임시 파일과 transaction 복구를 검사했다.
이전 Windows rename rollback 시험도 이번 실행에서는 제외하지 않고 통과했다.
처음 추가한 늦은 부팅 fixture의 USB 디렉터리 재생성 전제가 한 번 실패했고 이를 고쳐 최종 통과했다.
skip 4는 pyray 1, built Linux cereal/msgq 2, Linux flock 1이다. 기존 경고 19개는 남아 있다.
변경 Python의 Ruff·3.10 문법, Bash 구문, diff whitespace 검사를 통과했다.

전체 openpilot/C++ 빌드, 실제 L4T systemd/NM/권한·재부팅, TensorRT 빌드/정확도/지연,
TURZX/YOLO 동시 부하와 주행은 미실행이다. 사용자 ping 성공 외의 새 실기 성공을 주장하지 않는다.
MED/SCC/레이더/CAN·DBC/정지선/R·D/조향·제동/HUD layout/DM/model 입력·deadline·복구 정책 파일은 변경하지 않았다.
기능은 기본 OFF이며 수동 설치 후에도 comma 추론 opt-in은 별도다.

재현: 기존 [전체 회귀 명령](jetson_usb_status_gap_20261020.md#테스트-결과와-재현)에
`openpilot/tools/jetson/tests/test_protected_ncm.py`가 포함되도록 `openpilot/tools/jetson/tests`를 실행한다.
이번에는 `-k ...` 제외 옵션을 사용하지 않았다. CARROT_JETSON은 위 고정 SHA checkout이어야 한다.

Docs-Not-Needed: docs/user와 설정 Wiki는 변경하지 않고 요청한 보호형 설치/검증 개발 문서만 갱신했다.
