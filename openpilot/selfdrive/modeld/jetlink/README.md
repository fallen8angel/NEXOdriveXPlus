# NEXO experimental Carrot Jetlink port

This directory is an experimental, default-off NEXO adaptation of Carrot's
Jetson model-offload architecture.

Reference client architecture: `ajouatom/openpilot` commit
`eb5bbf2720286f17fc55d6eab8a2f7aacecf1f3d` (`carrot-wip`). Jetson server is
pinned by `install_jetson_server.sh` to `ajouatom/carrot-jetson` commit
`d0dc96fa32410403dfb2fefff3e2909c11ec109b`.

The JLNK v2 wire protocol is compatible with Carrot/Jetlink. NEXO reuses its
existing hardened FunctionFS transport. See `openpilot/tools/jetson/LICENSE.jetlink`
and `openpilot/tools/jetson/UPSTREAM.md` for provenance.

## Important difference from Carrot

This port does **not** replace NEXO's driving model with Carrot/Cinque. It hashes
and reads metadata from NEXOdriveXPlus's own
`openpilot/selfdrive/modeld/models/driving_supercombo.onnx`. The Jetson server
must build and serve that exact model. A SHA/model-contract mismatch refuses the
external session and leaves local modeld in use.

## Safety boundaries

- Default is OFF. `/data/nexo_jetlink_enabled` must exist before modeld starts.
- Existing `NexoJetsonUsb` display-only mode and Jetlink inference remain mutually
  exclusive USB owners. **Keep `NexoJetsonUsb=0` for inference + Jetson-attached
  HUD:** the native inference owner now carries both, with no second USB owner.
- No CAN, Panda, steering, longitudinal, SCC, MED, radar or DM code is changed.
- The normal NEXO model is loaded first and remains the fallback before joining.
- A new external session may join only after local warmup and while stopped or
  while the driver is physically pressing the steering wheel.
- If an already-active external inference link fails, modeld deliberately exits
  so the existing model communication watchdog can request disengagement. A
  manager restart returns to the local model path.
- Hardware validation and a full target Linux build are pending. Unit/integration
  checks do not establish live Jetson inference or panel operation.

## Jetson setup

On the existing Ubuntu/JetPack Jetson desktop, while parked, use the NEXO checkout
with built cereal/msgq and the existing HUD dependencies. The selected Python
must have TensorRT, libusb, PyUSB, NumPy, Pillow, pyray and PyAV. The installer
checks these and ffmpeg/libx264 before replacing services. Use the actual desktop
`DISPLAY` and readable `XAUTHORITY`; it does not invent a graphics login.

```sh
cd ~/NEXOdriveXPlus
git fetch origin NEXO
git switch NEXO
git pull --ff-only origin NEXO
export NEXO_ROOT="$PWD"
# Set PYTHON_BIN to the existing prepared Python if it is not python3.
bash openpilot/selfdrive/modeld/jetlink/install_jetson_server.sh
systemctl status nexo-jetlink-server.service nexo-jetson-hud.service --no-pager
```

The installer checks `usb1`, NumPy and TensorRT, pins the Carrot Jetson server,
builds a TensorRT cache from NEXO's own ONNX model, then starts the USB-host
server on `1209:0001`. The server composes the pinned Carrot `CarrotSession`
(read-ahead, TensorRT, navigation fragment forwarding) with a read-only NEXO HUD
snapshot consumer. The independent HUD service renders the existing NEXO
layout on supported TURZX `1cbe:0092` / `1cbe:0123` panels connected to Jetson.
It stops on missing dependencies instead of changing JetPack automatically.

Installation backs up existing NEXO service units and disables the alternative
`nexo-jetson-host`, `carrot-jetlink` and `carrot-jetlink-hud` services after checks
and model compilation pass, to keep one USB owner and one panel renderer. The
previous display-only installer is a separate mode and must not run alongside
native inference. Existing YOLO, camera ownership and vehicle controls are not
reconfigured by this installer.

## Shared inference + HUD link

`daemon.py` negotiates `nexo_hud_v1` plus Carrot display capabilities. Snapshot
serialization/preview generation and navigation fragments run in fresh, normal
priority children. The inference owner sends at most one 96 KiB HUD snapshot
at 10 Hz and two 32 KiB navigation fragments per turn, with a shared 12 ms USB
tail deadline, **after modeld has its result**. Workers do not write USB.

NEXO settings, filtered receive-only SPAS12 parking data, and existing camera
previews are retained; oversized optional route/diagnostic data yields first.
Legacy inline guidance images larger than 16 KiB are omitted from snapshots;
the independent navigation media channel remains available. Actual HUD
connect/disconnect edges feed the preview worker and trigger the existing
navigation bootstrap replay for late HUD startup/replug.
Navigation uses Carrot's ordered fragment consumer/keyframe recovery, with
timestamps translated from Comma to Jetson. A stock server lacking the NEXO
capability can still infer, but HUD transmission remains off with an explicit
installation hint. No upload, shutdown, model selection or remote setting
commands were added to the client.

Producer failure expires display data without failing inference. A USB transfer
failure invalidates the shared connection under the existing inference failure
policy; it must not be hidden after a partial write. Snapshots expire after
0.5 seconds on Jetson. HUD connection proof comes from recent actual panel
transmissions, separately from HUD data TX; both are reported by the 7000 page.

Physical checks still required: onroad readiness while parked; exact model SHA;
HUD speed/navigation/parking/camera layout; latency with inference active;
late HUD boot and panel unplug/replug; Comma/Jetson restart; shared USB loss.

## Experimental fallback for AGNOS g1 ownership (USB-NCM TCP)

The October 19 comma4 field log shows `g1` with `ffs.adb` and `ncm.0`
owning the only USB device controller. Carrot's dedicated FunctionFS Jetlink
gadget cannot coexist with this configuration without a disruptive USB handoff.

The optional `ncm-tcp` transport added in the separate experimental branch
reuses the existing `g1` NCM network without unbinding it. This is **not an
automatic fallback** and is **not enabled by default**. It requires an actual
Jetson USB NCM address of `192.168.100.250/24`, comma `usb0` at
`192.168.100.127/24`, and a Jetson server explicitly installed with
`NEXO_JETLINK_TRANSPORT=ncm-tcp`. The connection is rejected if the route is
not via `usb0`. The existing NEXO model hash, HELLO, engine checks, warmups
and optional HUD snapshot client remain in force. The Jetson-attached HUD still
needs its independent NEXO service.

Parked/offroad only, *after* the Jetson USB NCM host and SSH have been verified:

```sh
# On Jetson, after configuring its USB NCM interface and verifying the exact
# host setup (this installer restarts services):
NEXO_JETLINK_TRANSPORT=ncm-tcp \
NEXO_JETLINK_USB_IFACE="${VERIFIED_COMMA_USB_IFACE:?verify the cdc_ncm interface first}" \
NEXO_JETLINK_MODEL_SHA256="${VERIFIED_COMMA_MODEL_SHA256:?hash the comma ONNX first}" \
bash openpilot/selfdrive/modeld/jetlink/install_jetson_server.sh

# On comma, with usb0 already correctly configured:
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh enable-ncm-tcp

# Read-only diagnostics:
python3 openpilot/selfdrive/modeld/jetlink/diagnose_usb_owner.py

# Disable while parked/offroad if a test fails:
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh disable
```

**Do not run these commands until the experimental branch has been separately
reviewed and deployed on both devices.** The TCP protocol has no built-in
peer authentication; only the USB-point-to-point interface should be exposed.
This path has no hardware validation yet. It is not a claim of live Jetson
inference, acceptable driving latency, or HUD/YOLO operation. See
`USB_G1_COLLISION_KO.md` for the full Korean field log, Carrot comparison,
staged setup and recovery plan.

The October 20 review adds lifetime `SO_BINDTODEVICE` enforcement, direct-route
and active-NCM checks on both ends, a source-address allowlist, a bounded
nonblocking server transport and a restricted NCM command set. The Jetson unit
receives only `CAP_NET_RAW` for interface binding; the comma preflight refuses
enablement if its runtime cannot bind the interface. Do not grant capabilities
to a general Python executable. IP filtering is not peer authentication.
Addresses are a deliberately provisioned pair, not a claim about the current
Jetson address. Neither endpoint provisions persistent networking automatically.

The native model has `pad=(-2, None)` metadata; output allocation now uses its
ONNX output shape (2,576 floats), not slice endpoints. Detailed evidence,
remaining hardware gates and test results: `../../../../docs/jetlink_ncm_review_20261020.md`.

## Comma enable/disable

Only while parked/offroad:

```sh
cd /data/openpilot
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh enable
```

Check status:

```sh
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh status
cat /dev/shm/nexo-jetlink.json
cat /dev/shm/nexo-jetlink-model.json
cat /dev/shm/nexo-jetlink-spec.json
```

Disable only while parked/offroad:

```sh
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh disable
```

The original NEXO modeld source is preserved byte-for-byte as
`openpilot/selfdrive/modeld/modeld_local.py` in this branch.
