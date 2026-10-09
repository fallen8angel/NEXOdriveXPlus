# NEXO experimental Carrot Jetlink port

This directory is an experimental, default-off NEXO adaptation of Carrot's
Jetson model-offload architecture.

Reference baseline: `ajouatom/openpilot` commit
`eb5bbf2720286f17fc55d6eab8a2f7aacecf1f3d` (`carrot-wip`).  The JLNK v2 wire
constants and Cinque v2 model contract are intentionally interoperable with the
pinned Carrot/Jetlink implementation.  NEXO reuses its existing FunctionFS
transport, which was itself adapted from Jetlink under the MIT license; see
`openpilot/tools/jetson/LICENSE.jetlink` and `openpilot/tools/jetson/UPSTREAM.md`.

## Safety boundaries

- Default is OFF. `/data/nexo_jetlink_enabled` must exist before modeld starts.
- Existing `NexoJetsonUsb` display mode and Jetlink inference are mutually
  exclusive because both need the same USB device controller.
- No CAN, Panda, steering, longitudinal, SCC, MED, radar or DM code is changed.
- The normal NEXO model is loaded first and stays the fallback.
- A new external session may join only after local warmup and while stopped or
  while the driver is physically pressing the steering wheel.
- If an already-active external inference link fails, modeld deliberately exits
  so the existing model communication watchdog can request disengagement.  A
  manager restart then starts on the normal local model until a later safe join.
- This branch is not vehicle validated. Do not merge to NEXO before bench and
  parked-device checks pass.

## Enable while parked

```sh
cd /data/openpilot
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh enable
```

Start the next onroad session with the Jetson attached over the comma USB-C
host/device link.  The Jetson must already provide the pinned Cinque v2 Jetlink
engine (`09d080f3...22aec`). This first NEXO port intentionally does not upload
or build a missing model while driving.

Check status:

```sh
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh status
cat /dev/shm/nexo-jetlink.json
cat /dev/shm/nexo-jetlink-model.json
```

Disable only while parked/offroad:

```sh
bash openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh disable
```

Removing the marker restores the original `modeld_local.py` path on the next
modeld start. The original NEXO modeld source is preserved byte-for-byte as
`openpilot/selfdrive/modeld/modeld_local.py` in this branch.
