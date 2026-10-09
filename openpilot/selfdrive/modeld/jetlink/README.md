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
- Existing `NexoJetsonUsb` display mode and Jetlink inference are mutually
  exclusive because both need the same USB device controller.
- No CAN, Panda, steering, longitudinal, SCC, MED, radar or DM code is changed.
- The normal NEXO model is loaded first and remains the fallback before joining.
- A new external session may join only after local warmup and while stopped or
  while the driver is physically pressing the steering wheel.
- If an already-active external inference link fails, modeld deliberately exits
  so the existing model communication watchdog can request disengagement. A
  manager restart returns to the local model path.
- This branch is not vehicle validated. Do not merge to NEXO before Linux build,
  bench, parked-device and controlled-road validation pass.

## Jetson setup

On the existing Ubuntu Jetson checkout:

```sh
cd ~/NEXOdriveXPlus
git fetch origin
git checkout feature/carrot-jetlink-nexo-20261009
bash openpilot/selfdrive/modeld/jetlink/install_jetson_server.sh
systemctl status nexo-jetlink-server.service --no-pager
```

The installer checks `usb1`, NumPy and TensorRT, pins the Carrot Jetson server,
builds a TensorRT cache from NEXO's own ONNX model, then starts the USB-host
server on `1209:0001`. It stops on missing dependencies instead of changing
JetPack automatically.

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
