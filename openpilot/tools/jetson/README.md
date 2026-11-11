# Optional NEXO USB / Jetson HUD integration

**Hardware validation and the full Linux build are pending. Keep `NexoJetsonUsb=0`
until both pass. This branch is not a vehicle-tested release.**

For **native Jetson model inference and a HUD plugged into Jetson at the same
time**, use the combined native installer in
[`modeld/jetlink`](../../selfdrive/modeld/jetlink/README.md), with `NexoJetsonUsb=0`.
The display-only mode described below is an alternative USB owner.

## Baseline and scope

- NEXO baseline: `757102f3f0a18ec1533c4c46fa712ffbf96d7645`.
- Carrot reference and source attribution: [UPSTREAM.md](UPSTREAM.md).
- USB transport, heartbeat/reconnect, read-only HUD forwarding and an optional
  input selector for the existing direct YOLO process are included.
- No DM inference/state, Cinque, model offload, CAN transmit, steering,
  longitudinal control, MED, SCC, radar, stopping thresholds or navigation speed
  logic is changed. No model file, DBC, firmware or AGNOS update is included.

## Data flow and ownership

```text
Comma controls/modeld/camerad/Carrot Navi (existing processes)
  | subscribe only: cereal state, RX SPAS12, roadEncodeData, carrotNaviMedia
  v
nexo_jetson_usb (optional Python process, nice 10, default OFF)
  | FunctionFS USB device, own gadget only, bounded messages/write watchdog
  v
Jetson USB-A host: nexo-jetson-host.service
  | HUD snapshot                 | encoded road video
  v                              v
nexo-jetson-hud.service           existing nexo-yolo.service (--usb-input)
  | original NEXO renderer        | original HEVC/YOLO model and options
  | software libx264              v
  v                              display-only yolo.json mailbox
existing TURZX USB HUD
```

Host-to-Comma USB messages are restricted to heartbeat/telemetry. There is no
remote parameter setter, arbitrary command, update receiver or CAN publisher.
The only Jetson-local cereal publishers are `roadEncodeData` and
`carrotNaviMedia`. No new cereal schema or network service port is added.

The HUD adapter keeps the existing vehicle-state parser, SPAS12 decoder, BSM,
parking layout, camera crop/mirror/size settings and navigation renderer.
SPAS12 is filtered to receive bus 0/address 0x4F4/8 bytes; unrelated CAN and
`sendcan` are not forwarded. Both event time and navigation-display
`receivedMono` are translated between the two boot clocks. Expired snapshots
invalidate vehicle state instead of freezing the last valid car/parking state.

Road/wide and the existing NEXO side/reverse camera image are small JPEG display
previews (384 px width, at most 10 Hz). The side/reverse image comes from the
existing camerad DRIVER stream; no driver-monitoring algorithm or camera-device
owner is started. Preview compression/resolution and latency need physical
comparison against the current HUD. Main system statistics still describe
Comma; local renderer process diagnostics describe the Jetson renderer.

For a late-started HUD, Comma uses the **existing**
`CarrotNaviWebBootstrapRequest` display replay mechanism once on the connected
edge. Forwarded `web_image`/`web_render` envelopes are normalized only in the
USB copy. The original publisher, navigation receiver and 7000 web server are
unchanged. Cached media replay and map keyframe recovery need device testing.

## Startup, failure and fallback

1. Manager starts the optional daemon only on TICI when `NexoJetsonUsb` is true.
   Its gate does not modify any other process gate. It can wait offroad for a
   late Jetson; controls do not wait for it.
2. Comma must report a source/host on USB-C. Setup creates only its own
   `nexo-display` gadget and refuses another gadget's occupied UDC. Unsupported
   kernel/role/permissions remain a disconnected optional feature.
3. FunctionFS descriptors are written before binding, and endpoints are opened
   only after host configuration. Watchdogs abandon stalled writes and close
   the link. The host retries USB discovery without requiring a reboot.
4. Each connection has a new nonce and increasing heartbeat sequence. Replayed
   packets cannot renew a lease; missing heartbeats expire after two seconds.
5. USB video takes priority. The Wi-Fi video subscriber is closed while USB is
   live. On fallback/reconnect/gaps, wait for a HEVC keyframe; reset the existing
   decoder before consuming the new source. There is one YOLO process.
6. HUD and YOLO services are independent from the USB host service. A missing
   HUD waits; unplug/replug uses the existing USB error path and systemd retry.
   On ignition off, the HUD exits and waits for the next fresh onroad/debug state.
7. Wi-Fi fallback preserves the **existing video/YOLO path**. There was no
   existing remote Wi-Fi HUD state path to preserve. USB loss makes the remote
   HUD show unavailable/standby state; Comma control continues. The original
   Comma-attached HUD path remains available when physically connected there.

The existing status ports 8766/8767/8768 remain. The USB owner and legacy Wi-Fi
beacon have separate display leases so an absent USB link cannot erase a live
Wi-Fi badge. In USB YOLO mode the old YOLO launcher does not start its extra
beacon: the independent host service owns status. The badge layout is unchanged.

## Windows carrot-jetson and 7000 management

The existing optional vehicle USB owner also recognizes the Windows
[carrot-jetson image](https://github.com/ajouatom/carrot-jetson/blob/main/docs/INSTALL-WINDOWS-KO.md).
It listens first for the existing NEXD host announcement, otherwise performs a
JLNK v2 HELLO/STATE session. Each connection pins one protocol. The Carrot path
only reads telemetry and forwards existing HUD/navigation display snapshots;
it never requests an engine, uploads models or performs inference. Driver
monitoring is excluded. The NEXD video/YOLO path remains unchanged.

`NexoJetsonUsb` remains opt-in. When the native NEXO inference marker exists,
the display owner yields instead of claiming its controller. Native offload
still requires the exact NEXO model and its existing installation workflow;
a stock Carrot engine is not assumed to match it.

Both existing display USB owners retain three short attempts per failure
episode, then wait 30 seconds before a new episode. Healthy traffic resets the
budget. Valid saved cooldowns survive service restarts; corrupted retry records
remain blocked until reboot. This allows a late host/reboot to recover during
the same vehicle boot without changing manager or control process gates.

`/jetson` combines independent UDP and USB leases, current native offload state,
actual negotiated USB speed, frame/model readiness and error information.
Expired telemetry cannot retain a connected/ready indication. `/jetson/install`
is a separate Windows guide with the original guide link.

Logs, settings, model information, service restart and read-only update checks
use keyed SSH. Configure an existing SSH alias or `user@host` on the page, or
set `NEXO_JETSON_SSH_TARGET`. Register the public key and known host key using
the existing SSH configuration; no password/account/key is guessed or stored
by the page. Commands verify the target is Jetson and autodetect supported
systemd units. Restarts require explicit confirmation, current offroad Params
and inactive native model state. Missing safety state refuses the action.

The stock image need not provide a 5600 HTTP page. Its web link is shown only
after an HTTP response; it does not determine USB connectivity. The updater
only compares manifest metadata/signature and existing staged state; no update
activation, setup script, storage erasure, shutdown or power policy is exposed.
Automatic Wi-Fi profile provisioning and driving model migration are outside
this management adaptation; preserve the installed network configuration.

The Carrot companion preview uses the installed decoder's exact 384x240 format,
road only, at a maximum sampling rate of 5 FPS and a 16 KiB JPEG budget. Legacy
NEXD preview geometry and side/reverse display selection stay unchanged. Capture
timestamps come from VisionIPC EOF; old frames cannot acquire a fresh timestamp
by being encoded again. The camera worker reports generation FPS, process CPU
usage and preview latency. Snapshot sizing removes optional display events
before dropping the entire 96 KiB Carrot packet; no raw CAN or driver image is
added to this companion path.

The existing 7000 page and 8-second diagnostics share stage observations:
Jetson, USB, session/capability, camera TX, engine/inference and HUD. TX completion
does not prove reception or successful rendering. The SSH video observation
reads receipt metadata from the installed Carrot HUD packet without returning
JPEGs or vehicle snapshots. Host health expires independently of heartbeat.
Unknown temperatures, storage modes and runtime versions remain unknown.

Diagnostic-file failures do not terminate the host video loop or companion
STATE polling. Existing retry/USB role ownership remains unchanged; the
installed FUSB301 policy is observed without writing USB roles. Wi-Fi secrets
are not provisioned, placed in snapshots/Params, or printed by these summaries.
YOLO's existing display-only mailbox has explicit class/confidence/source/time
and source epoch metadata; it expires after source changes or stale reception.
It is never published into radar/model/control services.

## Device checks and explicit installation

Known HUD support in current code: VID `1cbe`, PIDs `0092` and `0123`.
A USB-C connector alone does not identify the panel. The installer does not
guess a carrier board, JetPack release, desktop session or Xauthority path.

On Jetson, record:

```sh
cat /etc/nv_tegra_release
lsusb
lsusb -t
printf 'DISPLAY=%s\nXAUTHORITY=%s\n' "$DISPLAY" "$XAUTHORITY"
python -m openpilot.tools.jetson.diagnose --role host
```

Use the existing prepared Jetson Python environment with built cereal/msgq,
PyAV, pyusb, libusb1, Pillow, numpy, repository-compatible raylib and ffmpeg.
No dependency/model version is automatically changed. The diagnostic command
reports missing imports. The Windows PyPI raylib used for local unit tests is
not a substitute for validating the actual Jetson runtime.

After Linux build/device review, installation is explicit:

```sh
sudo DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" \
  bash openpilot/tools/jetson/install_host.sh /absolute/repo USER /absolute/python COMMA_IP 0
```

Use `-` instead of `COMMA_IP` for USB only; no Wi-Fi address is assumed.
The last argument `0` installs USB/HUD only. `1` additionally requires the
existing `nexo-yolo.service`, sets `NEXO_USB_VIDEO=1` in its existing user config
(with a backup), and enables the host's local encoded-video publisher. It does
not change `best.pt`, YOLO thresholds or model execution settings.

USB discovery also checks the product string `NEXO display link` and requires
SuperSpeed. The benchmark test VID/PID is experimental; see UPSTREAM.md.
For updates, deploy matching code on Comma and Jetson while parked, then restart
only these optional Jetson services. Incompatible protocols are rejected. No
Carrot remote model/update/signing infrastructure is included.

To revert: turn off `NexoJetsonUsb` on Comma; set `NEXO_USB_VIDEO=0` in the Jetson
YOLO config and restart its existing service, then stop/disable the two
`nexo-jetson-*` services. This returns YOLO to the original remote Wi-Fi source.
Vehicle model/control processes require no change.

## Verification boundaries

Unit tests cover fragmented I/O, caps, protocol rejection, write watchdog,
heartbeat replay/reboot/staleness, single-source video selection, keyframe
recovery, typed display Params, clock translation, SPAS12 data/expiration,
read-only messages, default-disabled registration and YOLO output sanitation.

Run on the built Linux checkout:

```sh
pytest -q openpilot/tools/jetson/tests
scons
```

On this Windows workstation the unbuilt native `params_pyx`/msgq prevents the
repository's normal pytest initialization, and `scons`/Linux are unavailable.
Pure/capnp tests are run with `--confcutdir` and `-o addopts=''` to bypass that
environment initialization/absent pytest plugins; test assertions are unchanged.

The baseline has a known independent test discrepancy:
`test_cluster_reverse.py::test_spas_rear_positions_are_mapped_to_display_stages_and_expire`
expects expiration at 1.01 seconds, while the current tracker uses 1.5 seconds.
It fails identically on baseline 757102f3. Neither this test nor the tracker was
modified for this work.

Physical checks remain mandatory: no devices; HUD only; Jetson only; both;
late boots; each cable unplug/replug; host/HUD/YOLO process kill; Comma/Jetson
reboot; ignition transitions; steady-state CPU/temperature; no control deadline
regressions; R/D parking freshness; BSM; side-camera crop/size/mirror; Carrot
Navi/vNAVI/media recovery; USB versus Wi-Fi video exclusivity; badge expiration.
No physical success is claimed from mock tests or Python compilation.

## Jetson 외부 HUD 기어 화면

7000의 **화면 표시 → 외부 HUD → 화면·카메라 → 외부 HUD P단 차량 화면**에서 설정합니다.
기본값은 사용이며 콤마의 영구 Params에 저장합니다. 콤마와 Jetson은 같은 NEXO
커밋으로 업데이트합니다. 7000의 Jetson HUD 업데이트에는 차량 이미지도 포함됩니다.

P단의 은색 1세대 넥쏘는 생성한 2.5D 이미지입니다. 방향지시등은 유효한
`carState`를 따릅니다. HUD 하단의 전송 상태는 최근 패널 쓰기 완료를 가리킵니다.
R단은 수신한 SPAS12 감지 단계를 앞·뒤 각 다섯 위치에 표시합니다.
실제 후방 영상이 없으면 연결 안내를 표시합니다.

2026-11-10 변경: R단 외부 HUD의 영상 입력을 별도 UVC 후방 카메라에서
**콤마4의 기존 실내(운전자 방향) 카메라**로 변경했습니다. R단에서는
`camerad`의 읽기 전용 `VISION_STREAM_DRIVER` 미리보기를 콤마가 기존
카메라 전송 경로로 Jetson에 보내고, 기존 `ReverseCameraSession` /
`RemoteCamera(driver=True)`로 약 384픽셀 폭의 JPEG 영상을 표시합니다.
카메라 장치에 새로 연결하거나 운전자 모니터링/DM을 새로 실행하지 않습니다.
기어가 R이 아닐 때는 해당 텍스처를 해제합니다. 영상이 300ms보다 오래되면
미수신 안내로 바뀌고 실내 영상이나 지난 화면을 후방 영상처럼 남기지 않습니다.

**주의:** 콤마4 실내 카메라는 운전자를 향합니다. 차량 뒤쪽 시야를 제공하지
않으므로 HUD에는 '실내 카메라 · 후방 시야가 아닙니다'라고 표시합니다.
후진할 때 차량의 순정 후방 카메라, 거울 및 주변을 직접 확인해야 합니다.
제트슨의 별도 UVC `/dev/nexo-hud-rear` 입력은 이 R단 화면에서 더 이상
사용하지 않습니다. R단의 SPAS12 주차 센서 표시는 그대로 유지합니다.

### 2026-11-11 R단 오른쪽 주차 보조 화면 개선

콤마4 실내 카메라(왼쪽), 기어 전환 판단, 영상 선명도·미러링,
영상 미수신 안내 및 카메라 텍스처 해제 경로는 **그대로 유지합니다**.
외부 HUD 오른쪽의 기존 '주차 센서' 2행 네모 막대만 다음처럼 변경합니다.

- 중앙에는 저부하 2D 넥쏘 탑뷰를 그립니다. 별도의 이미지/3D 모델,
  GPU 텍스처 생성 및 외부 네트워크 요청은 추가하지 않습니다.
- 기존 SPAS12 수신을 이용하여 전방 다섯 구역과 후방 다섯 구역을
  차량 그림 주위와 오른쪽 작은 상태 막대에 동일한 순서로 표시합니다.
  수신 단계 1은 초록, 2는 노랑, 3은 빨강이며 0/미확인은 회색입니다.
- 가장 높은 수신 단계의 방향을 '전방/후방 좌측·중앙·우측 감지'로
  표시합니다. 같은 단계라면 후방을 우선합니다.
- 유효한 조향각은 좌/우 각도 정보 및 짧은 **참고선**으로 표시합니다.
  이 선은 넥쏘 기하학으로 보정된 주차 경로나 실제 후방 영상이 아닙니다.
- 차량 속도는 표시만 하며 CAN이 미수신이면 '센서 정보 미수신'을
  표시합니다. 센서 0값은 안전확정이 아니라 '감지 표시 없음'입니다.

SPAS12 단계값을 거리(m)로 임의 환산하지 않으며 실제 후방 장애물을
식별하는 AI 기능이나 음성 경보도 추가하지 않습니다. 반드시 순정 후방
카메라와 주변을 확인해야 합니다. 외부 HUD 검은 화면 이력이 있으므로
정차 시험에서 카메라 지연·화면 전환·전체 프레임 연속성을 검증하십시오.
실차 및 GPU 렌더링 검증은 별도로 필요합니다.

### 2026-11-10 당근파일럿 P단 화면 선택적 벤치마킹

당근파일럿 `carrot-wip`의 P/R 전용 화면(커밋 `c8454993d2`)을 검토했습니다.
NEXO는 이미 P/R/D 자동 전환, 넥쏘 사진 및 후진 카메라와 주차 센서를
구현했으므로 EV6 모델과 차종별 후진 기하계산은 가져오지 않습니다.

기존 NEXO P단 화면의 오른쪽에 읽기 전용 수소 잔량(`carState.fuelGauge`),
타이어 공기압, 이번 주행의 거리·시간·평균속도·자동주행 비율을 표시합니다.
센서가 없거나 최근 `carState`가 유효하지 않으면 `--`로 표시하며
수소 주행 가능 거리 등 근거가 없는 값은 추정하지 않습니다.
주행기록은 기존 `cluster_trip_report` 계산값을 재사용하고 통신
프로토콜, CAN 명령, 기어 판정, 차량 제어 코드는 변경하지 않습니다.

기어 입력이 짧게 끊길 때는 1초까지 기존 화면을 유지하지만,
이후에도 물리 기어 증거가 복구되지 않으면 일반 주행 렌더러로
되돌아갑니다. 화면만 되돌리는 것이며 차량 기어를 바꾸지 않습니다.
당근의 주차 날씨 온라인 요청은 HUD 끊김 문제에 영향을 줄 수 있어
도입하지 않았습니다. 기존 넥쏘 사진 텍스처는 P단 최초 진입 때만
로딩하여 이후 재사용합니다.

이 변경은 코드/단위시험 수준이며 실차 검증을 의미하지 않습니다.
먼저 P에서 표시값, P↔D/R 전환, 입력 지연 1초 후 복귀,
후방 카메라 및 외부 HUD 검은 화면 재발 여부를 확인하십시오.
