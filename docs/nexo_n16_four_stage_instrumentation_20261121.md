# NEXOdriveXPlus — Carrot N16 four-stage OFFROAD development gates

This change adds software for all four requested priorities, **without**
claiming that Carrot N16 already runs on the 8 GB Jetson. The only on-device
N16 builds reported to date have failed from CUDA out-of-memory or the
2 GiB MemAvailable safety guard. The installed modeld, CAN, exterior physical
HUD, live NCM, protected engine caches and 2019 NEXO gear UI are untouched.

## 1. GPU/shared-memory diagnosis

The opt-in `nexo_n16_trt_trial` now records a bounded 0.5-second-sampled
memory timeline for its worker and short-lived subprocesses, with a maximum of
256 retained rows. Included metrics: system MemAvailable / MemFree / CmaFree /
SwapFree, active worker RSS/summed high-water and stages observed from the
worker status file. We deliberately call this **observed unified/host RAM**:
Jetson CPU and GPU share memory, and this is **not** a measured peak of
CUDA allocations. Status is written to:

- `/opt/carrot-jetlink/cache/nexo-n16-trial-result.json`
- `/opt/carrot-jetlink/cache/nexo-n16-trial-memory.json`

The read-only `nexo_n16_diagnostics --status` summarizes the latest
result, feasibility assessment, memory timeline and HUD observer gate.
The 7000 `젯슨 통합진단 ZIP` includes read-only
`remote/16_n16_trial_status.json` and
`12_n16_gpu_memory_and_hud_observer.{json,txt}`.

## 2. Alternative, bounded build memory path

A new explicit **`--split-builder`** option patches the exact SHA-pinned
original N16 ONNX in a separate, short-lived CPU-only Python process. That
process exits and releases its Python ONNX model allocations before the
TensorRT builder is imported. The next isolated worker sees the prepatched
ONNX file by a hardlink rather than re-reading/re-saving 815 MB inside the
builder's Python process. Original model weights and all graph arithmetic stay
unaltered. The prepatched file and temporary TRT plan are stored only inside
the existing disposable experiment directory; parent cleanup removes them.
No swap change, service shutdown, power-mode override, engine cache write,
car CAN or HUD change occurs.

This is an **experimental mitigation**, not an assurance that the 8 GB board
can build or execute a full N16 engine. It requires additional staging disk
space and may still be stopped by the unchanged 2 GiB memory safety guard.
Do NOT use split-builder on the currently installed pre-update version.

## 3. Feasibility and accuracy validation gate

`assess_feasibility` records the measured available-RAM drop and classifies
the last failed or successful isolated trial. `hardware_fit_proven` is false
unless an actual synthetic engine inference passed. CPU/GPU numerical parity,
camera calibration, physically displayed geometry, and actual NEXO vehicle
validation are each still false, **even if synthetic inference passes**.
No engine is made available to production modeld or controllers.

## 4. Passive N16 HUD observer contract only

`nexo_n16_hud_observer.py` checks the exact 18,580-element N16 output
contract and reports finite-value statistics for raw `lane_lines`,
`road_edges`, `lead`, `lead_prob` and `plan` sections. It never
projects raw vectors to pixels, draws on an exterior HUD, emits CAN, consumes
a live vehicle camera, or claims a traffic-light detector: the N16 output
contract does NOT expose an explicit traffic-light detection head.

The default result has `renderable_objects=[]`,
`physical_hud_feed_enabled=false` and `vehicle_control_enabled=false`
by construction. A future stage must first validate GPU inference, CPU/GPU
numeric parity, NEXO camera processing, camera-to-HUD geometry and live output
freshness before someone could design a separately reviewed HUD renderer.

## After a protected release containing this commit is installed

Run only with the engine/HUD still in display-only mode, strict Jetson→Comma
SSH using the existing approved key, Comma4 IsOffroad=1, IsOnroad=0 and external
AI disabled. **Do not run while driving or use P gear alone as Offroad proof.**
Inspect current available memory and disk space before any experimental trial.
One optional split-build attempt is:

```sh
ssh -T jetlink@192.168.100.250 '
cd /opt/carrot-jetlink/current &&
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
  --comma-ssh comma@192.168.100.127 \
  --identity-file /home/jetlink/.ssh/nexo_comma_guard_ed25519 \
  --workspace-mib 256 --optimization-level 0 --samples 2 --split-builder
'
```

**Do not repeat failed memory builds automatically.** Analyze the recorded
reason and memory timeline first. A successful build is only evidence of
synthetic GPU capability. It does not authorize NEXO driving or physical HUD
predictions.

Read-only status, no new inference:

```sh
ssh -T jetlink@192.168.100.250 '
cd /opt/carrot-jetlink/current &&
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_diagnostics --status
'
```

The tests use synthetic ONNX and sample vectors; CI has no real Orin GPU and
cannot prove an on-device TensorRT engine fit, numerical parity or real HUD.
