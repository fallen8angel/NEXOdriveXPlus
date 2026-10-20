# NEXO 보호형 Jetson: Identity → Cast 변환 수정

## 원인과 수정 범위

기준 NEXO 커밋은 `d23bad0a0e63117ef830a0091562e6971a616197`,
고정 Carrot 서버는 `d0dc96fa32410403dfb2fefff3e2909c11ec109b`이다.
실제 NEXO ONNX를 고정 `patch_uint8_inputs()`에 전달해 사용자가 보고한 오류를 재현했다.

```text
img     → bridge_img     (Identity) → vision/img     → vision/node__to_copy   (Cast FLOAT16)
big_img → bridge_big_img (Identity) → vision/big_img → vision/node__to_copy_1 (Cast FLOAT16)
```

Carrot의 `_head_casts()`는 입력 바로 뒤의 Cast 또는 입력 Concat 바로 뒤의 Cast만 찾는다.
NEXO의 Identity bridge는 인식하지 못한다. 원본 모델이나 고정 vendor 파일을 바꾸지 않고,
NEXO host/probe 프로세스에서만 `jetlink.onnx_patch.patch_file`에 검증된 adapter를 설치한다.
vendor 소스 SHA도 검사하므로 다른 버전으로 조용히 적용되지 않는다.

`onnx_inputs.py`는 두 UINT8 입력의 모든 소비 경로를 추적한다. Cast 이전에는 값 보존
Identity와 이미지 입력만 받는 Concat만 허용하며, 모든 경로는 FLOAT16 Cast로 끝나야 한다.
다른 연산·불완전한 경로·다른 Cast 대상·UINT8 graph output 노출·중첩 graph는 거부한다.
검사를 생략하거나 예외를 무시하지 않는다.

검증된 복사본에서 입력과 경로상의 value_info를 FLOAT16으로 바꾸고 해당 head Cast만
FLOAT16 Identity로 바꾼다. Cast의 출력 이름을 유지하므로 graph output이나 value_info가
생산자를 잃지 않는다. 나머지 노드·가중치·비영상 입력·출력 계약은 그대로 유지한다.
원본/변환본에 ONNX full checker를 실행하고 실제 head graph를 CPU reference evaluator로 비교한다.
빌더가 요청하는 임시 `patched.onnx`만 저장하며 원본 경로로 저장하려 하면 거부한다.

## 모델과 수치 검증

모델 SHA256는 변하지 않았다.

```text
f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5
```

원본은 60,792,584 bytes, img/big_img `[1,12,128,256]` UINT8,
최종 출력 `[1,2576]` FLOAT32다. 엔진 빌드용 복사본의 영상 입력만 FLOAT16이 된다.

- 실제 NEXO head 경로에 0~255 모든 UINT8 값을 입력해 FP16 비트 단위 일치를 확인했다.
- 실제 NEXO graph의 Concat·Sub·Div 정규화 구간까지 추출해 원본과 변환본을 실행했다.
- 변환본에는 고정 Carrot `RingQueue`의 실제 UINT8→FP16 LUT 경로로 만든 버퍼를 입력했다.
- 결과는 FP16 비트 단위 동일, 최대 절대 오차 **0**이다. 이는 전체 모델/GPU 출력의 동등성 주장은 아니다.
- 전체 모델 checker와 strict type/shape inference, 모든 가중치·다른 노드·출력 계약 보존을 검사했다.

배포 builder가 CPU 사전 검사를 수행하고 결과를 JSON manifest `onnx_preflight`에 기록한다.
Jetson 후보 probe도 EngineHost 요청 전에 동일 검사를 수행하며
`NEXO_ONNX_PREFLIGHT_OK`를 출력한다. 실제 Carrot TensorRT build 경로도 parser/Builder 생성
전에 검사를 거친다. 이 marker는 실제 엔진 빌드 성공을 의미하지 않는다.

## 변경 파일

| 파일 | 변경 |
|---|---|
| `openpilot/tools/jetson/onnx_inputs.py` | 경로 검사, 타입 일관성, 실제 head 수치 비교, 고정 vendor hook |
| `openpilot/tools/jetson/native_host.py` | 서버 main 실행 전 adapter 등록 |
| `openpilot/tools/jetson/protected_probe.py` | 엔진 준비 전 ONNX 사전 검사 |
| `openpilot/tools/jetson/build_protected_bundle.py` | 배포 전 사전 검사와 manifest 결과 |
| `openpilot/tools/jetson/tests/test_onnx_inputs.py` | 실제 모델·고정 builder·서비스 hook 포함 18개 회귀 |
| 이 문서 | 오류 원인, 검증, 새 설치/복구 명령 |

`protected_install.py`의 트랜잭션/자동 복구, USB-NCM, HUD 구현은 변경하지 않았다.
MED/SCC/레이더/CAN/DBC/차량 제어/운전자 감시/모델 파일도 변경하지 않았다.
기존 보호형 APP는 보존하고 차량 추론 기본 OFF 정책을 유지한다.

## 자동화 검사 결과와 한계

최종 전체 회귀: **426 passed, 4 skipped, 0 failed, 0 deselected**.
skip은 pyray 1개, built Linux cereal/msgq 2개, 실제 Linux flock 1개다.
Ruff·Python 3.10 문법·git whitespace 검사도 통과했다.
개발 환경은 Windows/Python 3.12/ONNX 1.23.2이며 기존 경고 19개가 있다.

최초 새 수치 테스트는 원본에 `vision/div` value_info가 없어 추출에 실패했다.
ONNX shape inference로 타입/shape를 얻은 뒤 실제 구간을 추출하도록 수정했다.
첫 전체 실행은 기존 복구 테스트 2개가 실패했다(하나는 Windows 임시 디렉터리 rename의
WinError 5, 하나는 복구 후 서비스 상태 assertion). 기존 복구 코드 변경 없이
해당 모듈 28개 개별 실행과 새 임시 경로의 전체 재실행이 모두 통과했다.
이 실패들을 GPU 오류 해결이나 실제 장비 복구 성공으로 해석하지 않는다.

**실제 Jetson GPU의 TensorRT 엔진 생성은 아직 검증하지 않았다.**
실제 ONNX parser/GPU tactic 선택·엔진 수치 정확도·지연, Jetson 재부팅/USB 재연결,
TURZX 동시 화면·부하 및 주행 검증은 별도로 필요하다.
이번 작업에서 장치 SSH·설치·차량 추론 활성화는 실행하지 않았다.

## 새 패키지 적용

기존 실패 설치가 rollback을 마쳤는지 먼저 확인한다. 정차·오프로드에서 Jetson 터미널:

```sh
cat /opt/carrot-jetlink/updates/nexo-install-status.json
test ! -e /opt/carrot-jetlink/updates/nexo-install-transaction.json
readlink -f /opt/carrot-jetlink/current
systemctl status carrot-jetlink carrot-jetlink-hud --no-pager -l
```

transaction이 남았다면 먼저 아래의 중단 복구 절차를 완료한다. 모델/engine cache나
transaction을 임의 삭제하지 않는다. 기존 Carrot server/HUD가 active여야 재설치가 진행된다.
APP read-only, DATA bind mount, Python3.10/TRT10.3.0/L4T36.4.7 전제는
[기존 보호형 설치 안내](jetson_protected_ncm_20261020.md)와 같다.

새 `NEXO_Jetson_protected_NCM_ONNX_20261021.zip`을 PC에서 압축 해제한다.
Jetson에서 수신 폴더를 만들고 PC에서 다섯 파일을 전송한다.

```sh
sudo install -d -o jetlink -g "$(id -gn jetlink)" -m 0750 /opt/carrot-jetlink/updates/nexo-onnx-incoming
```

```powershell
scp NEXO-protected-ncm-onnx.tar.gz NEXO-protected-ncm-onnx.json protected_install.py protected_ncm.py SHA256SUMS jetlink@192.168.45.196:/opt/carrot-jetlink/updates/nexo-onnx-incoming/
```

Jetson에서 SHA256SUMS의 네 파일이 모두 OK인지 확인한 뒤 설치한다.
comma에서 확인한 NEXO 모델 SHA가 아래와 동일해야 한다.

```sh
cd /opt/carrot-jetlink/updates/nexo-onnx-incoming
sha256sum -c SHA256SUMS
BUNDLE_SHA=$(/usr/bin/python3 -c 'import json;print(json.load(open("NEXO-protected-ncm-onnx.json"))["bundle_sha256"])')
sudo /usr/bin/python3 protected_install.py install \
  --bundle "$PWD/NEXO-protected-ncm-onnx.tar.gz" --sha256 "$BUNDLE_SHA" \
  --ncm-interface enx36770f430ea2 \
  --model-sha256 f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5 \
  --confirmed-offroad
```

`NEXO_ONNX_PREFLIGHT_OK` 다음 실제 엔진 생성·3회 출력 계약 검사·전용 TCP listener 확인을
통과해야 `NEXO_INSTALL_OK`가 나온다. 서비스 active만으로 성공을 판단하지 않는다.
설치 성공 후에도 comma 차량 추론은 별도 opt-in이며 자동 활성화하지 않는다.

```sh
cat /opt/carrot-jetlink/updates/nexo-install-status.json
cat /run/nexo-ncm-status.json
journalctl -b -u carrot-jetlink -u carrot-jetlink-hud -u nexo-jetson-ncm --no-pager -n 150
```

## 복구

설치 단계 실패 시 기존 installer가 이전 release/model/network로 자동 복구한다.
정상 설치 후 이전 환경을 다시 선택하려면 오프로드에서:

```sh
sudo /usr/bin/python3 /opt/carrot-jetlink/updater/nexo_protected.py rollback --confirmed-offroad
```

설치가 중단되어 transaction이 남은 경우, 오프로드에서:

```sh
sudo systemctl stop carrot-jetlink.service carrot-jetlink-hud.service
sudo /usr/bin/python3 /opt/carrot-jetlink/updater/nexo_protected.py activate
sudo systemctl start carrot-jetlink.service carrot-jetlink-hud.service
```

복구 명령이 실패하면 후속 start를 진행하지 말고 상태 JSON과 오류를 보존한다.
자동 복구 성공과 새 NEXO GPU 엔진 생성 성공은 서로 다른 결과다.
