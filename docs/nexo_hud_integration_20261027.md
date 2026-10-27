# NEXO HUD integration review — 2026-10-27

## Baselines and boundaries

- NEXO at start: `8814e28cfa70444730f62fb93fb7fdd82637e402` (rollback reference, not newly hardware-validated).
- carrot-wip: `f29102fa8346ca3c83eace15ee2915eed90015c9`.
- carrot-nav: `c1a28aff4bc0fc9ec7299ab01a0fb4f5c4a4cf43`.
- Installed-source contract retained: carrot-jetson `d0dc96fa32410403dfb2fefff3e2909c11ec109b`.
- Existing NCM receipt/recovery branch `codex/jetson-carrot-recovery-20261021` is already an ancestor of NEXO; its functionality is retained, not reimplemented.
- No device access, driving, DM changes, CAN/DBC, MED/SCC, radar or control changes. No network provisioning, ADB disconnect, model/runtime replacement, timeout extension or inference transport tuning.

Actual incident logs have **not** been supplied. Code defects and simulated evidence below do not establish the root cause of the user's physical HUD outages.

## Group 1: evidence, recovery and USB

The existing supervisor holds a renderer lock, shares the pinned panel owner, exits on stale snapshots, releases a session, and backs off (up to 60 seconds after repeated failures). A valid snapshot permits readmission. NCM sessions retain sequence/receipt checks and HELLO/idle deadlines.

Confirmed code issues addressed:

1. A successful panel write followed by an `OSError` storing HUD telemetry could previously terminate the renderer. HUD and NCM diagnostic writes now fail independently; required snapshot storage still fails the session, as acknowledging an unstored snapshot would be incorrect.
2. The logger inspected only `usb0`. It now reuses passive active-gadget NCM discovery. Missing/ambiguous visibility is unknown, not invented physical cable failure.
3. Wi-Fi beacons could contribute to HUD/NCM health. The long logger keeps beacon counts separate from its USB evidence.
4. `HUD_CONFIRMED` promoted software evidence to physical confirmation. The new classification is `HUD_REPORTED`; physical output stays unverified in logs/report/UI.
5. Two-sample debounce missed single observations. These are now separate `fault_observation` events, without inflating confirmed dropout counts. NCM transition events survive in a bounded 32-entry source history and are deduplicated by the logger.
6. Bounded 32-sample prehistory and 15-second post-incident samples retain context without increasing the 0.5-second polling rate. Disk caps remain 32/8 MiB. Storage errors cannot escape the logging tick.
7. Reopening a log appends instead of truncating. Complete transition counters are reconstructed; malformed records are counted, preserved and separated from subsequent lines. Reports skip corrupt lines instead of abandoning the remainder.

The report distinguishes observed recoveries from **automatic recovery success/failure**, which remains unknown without sufficient supervisor evidence. An unresolved outage is not automatically a recovery failure. Source errors and state classifications are observations, not established physical causes. Renderer PID and host panel-send time, source generation and local TX time are recorded where available; clocks from different devices must not be subtracted.

Remaining diagnostic limitations: physical panel visibility, encoder process/failure telemetry, exact device enumeration and first causal component are not fully observable. Polling cannot detect every sub-0.5-second physical interruption; the NCM event history covers only observed software transitions and is lost on process death. At storage limits, additional context is unavailable. Existing snapshot expiry (0.5 s), sender freshness (0.3 s), state response deadline (0.5 s), HELLO (3 s) and idle deadline (5 s) are retained. A renderer may legitimately return to its waiting screen when a snapshot expires despite a carrier remaining up. Timing correlation from actual logs is still required to determine why the snapshot stopped arriving.

Carrot's bounded FunctionFS request splitting, deferred completions, cleanup gate and opt-in 8 KiB AIO requests target a different transport. NEXO already uses nonblocking TCP, independent read/write deadlines, TCP_NODELAY, bounded framing, partial-message retention and closure on failed writes. No unmeasured USB optimization was imported. Hardware throughput improvement is **unmeasured** and must not be claimed.

## Group 2: display-only options

- Selectively adapted `64806b0`: `--usb-h264-ffmpeg-threads 0..16`. Zero remains the automatic default. Only libx264 encoder/conversion pools change; dimensions, FPS, bitrate, GOP and other encoders retain their settings. Returning to zero restores the original policy.
- `NEXO_HUD_SIGNALS=1`: uses existing `/dev/shm/carrot_signal_observation.json` observations. Valid road image dimensions, finite bounds, frame/session identity, current detection age and red/green raw agreement are required. Unknown detected signals are gray with `?`; missing/stale detections disappear. No detector or stop assistance is enabled.
- `NEXO_HUD_OBJECTS=1`: uses the existing bounded YOLO mailbox. Car/person/motorcycle icons appear only for valid current detections. The inset explicitly says `CAMERA 2D`; it is image position, not a distance or a calibrated 3D scene. Default OFF independently of signals.
- The inset is drawn in the Jetson-only world layer before the existing speed/navigation/parking/alerts layer. Optional draw errors cannot stop the HUD. Physical layout/legibility on all panel modes remains a device check.

These environment options belong to the existing Jetson HUD service. Do not enable them in vehicle control processes. Source snapshot additions require the comma update; renderer/encoder options require the Jetson update too. No new Jetson package is required by these production changes. Desktop benchmark/test dependencies are not installation requirements.

## Group 3: process and resource lifetime

- Selectively adapted GPS isolation from `2fe2caf`. Only `qcomgpsd` opts into multiprocessing `spawn`; all other process factories and start predicates retain their behavior. GPS computations and navigation are untouched. Real subprocess tests cover crash/restart, duplicate-start prevention, isolated parent state and supervisor stop.
- Adapted `deb63e2`: EGL import ownership ledger; idempotent destruction invalidates FD/image before native cleanup; cleanup continues after one image error; application releases camera imports before closing the display and handles normal termination signals. Tests use real FDs with native EGL doubles, not real camera hardware.

## Reproducible validation

Windows desktop, Python 3.12; use repository root and `opendbc_repo` on PYTHONPATH and CARROT_JETSON pointing to the exact pinned checkout. Native Linux dependencies are not available on this desktop.

```sh
python -m pytest -q -o addopts='' -o asyncio_mode=auto \
  --confcutdir=openpilot/tools/jetson/tests \
  openpilot/tools/jetson/tests \
  openpilot/system/manager/test/test_python_process_spawn.py \
  openpilot/selfdrive/ui/tests/test_egl_shutdown.py \
  openpilot/selfdrive/carrot/tests/test_cluster_ffmpeg_threads.py \
  openpilot/selfdrive/carrot/server/tests/test_nexo_hud_link_log.py \
  openpilot/selfdrive/carrot/tests/test_nexo_sensor_log.py \
  openpilot/selfdrive/carrot/tests/test_cluster_blindspot_road.py \
  openpilot/selfdrive/carrot/tests/test_cluster_parking.py \
  openpilot/selfdrive/carrot/tests/test_cluster_side_camera.py
python -m openpilot.tools.jetson.bench_hud_local --seconds 120 --output hud-local-benchmark.json
```

The local suite passed 409 tests, with 4 platform/native-extension skips and 29 unittest subtests passed. The 500-cycle real localhost TCP test and separate 500-cycle simulated log-count test are distinct. Real FFmpeg tests encode/decode every measured frame and check asymmetric color/orientation. Do not call the simulated 1500 seconds of logger time an actual 25-minute soak.

Earlier attempts exposed missing sparse-checkout modules, missing dependencies, Windows default text encoding, sandbox temporary-directory restrictions and an obsolete NCM test mock. The mock now targets `connect_display_ncm`, the actual production entry point; assertions were retained. The sensor logger integration now loads the real passive HUD module without importing all unrelated web routes, and checks HUD archive contents. The initial soak harness treated a 100 ms polling timeout as fatal; it was corrected to retry exactly as production `receive_session` does, without changing a production deadline.

GitHub tests.yaml now explicitly includes HUD long-log tests, an isolated Linux fault/resource job, development-branch push and NEXO PR triggers. Full release-build and main CI results must be checked before claiming integration or merging.

## Installation, backup and rollback

No installation was performed. Installed Jetson service/version, actual dependency versions and signed distribution status are unknown until the owner collects them. Retain the current runtime/model and existing signature/integrity checks; this source branch is not a newly signed installation image.

Before any parked-device update, save `git status`, branch/SHA, both staged and unstaged binary diffs, and a separate archive of untracked/locally modified files to storage outside the checkout. Back up existing Jetson service unit files and active release metadata. Verify the backups can be read; do not use reset/clean or overwrite the earlier reported local vehicle modifications. Compare each local diff with the candidate before applying it.

Only after CI and backup/conflict review, use the existing installation path for that device. Do not substitute the legacy installer for a protected installation. The repository's protected installer retains its `rollback --confirmed-offroad` action; confirm the actual installed program path and previous release before invoking it. Signed paths must continue through their existing verification flow. No guessed device-specific install command is safe to prescribe while service layout is unknown.

For repository rollback, retain baseline `8814e28cfa70444730f62fb93fb7fdd82637e402`. Revert the integration commit(s) on a separate branch, rerun CI and merge the revert; never force-push NEXO or discard vehicle-local edits. If the candidate was not merged, NEXO needs no rollback.

## Parked-device checklist (실차 검증 필요)

1. Record comma/Jetson SHA, active service ExecStart, runtime/model identity and backup local changes.
2. Check USB-NCM interface/carrier and actual session separately; preserve ADB and existing routes.
3. Visually verify real HUD output, existing navigation/speed/parking/R-D/blinkers and optional overlay placement.
4. Observe permitted disconnect/reconnect and ensure one renderer/panel owner; never restart vehicle controls for HUD recovery.
5. Start the existing long logger on port 7000 and observe for a representative duration.
6. On an outage, retain source events, before/after samples and manual physical-screen observations with time.
7. Stop logging and download `/download/nexo-long-log-latest.tar.gz`; report is `/view/nexo-long-log-report.txt`.
8. Storage: `/data/media/nexo-long-logs/`; latest package `/data/media/nexo-long-log-latest.tar.gz`.
9. Compare earliest abnormal evidence, generator/TX/receipt/panel-send timing and restart identity. Mark missing evidence unknown; an automatic recovery alone does not establish a root-cause fix.

Multi-hour soak, actual Jetson CPU/GPU/USB performance, installed-version compatibility, physical HUD output and actual vehicle nonregression remain **실차 검증 필요**.
