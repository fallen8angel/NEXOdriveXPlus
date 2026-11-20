# Carrot N16: 2019 NEXO Offroad TensorRT isolated GPU trial

The N16 ONNX source is the exact Carrot model, not a new/retrained model.
Size 814591107 bytes; SHA256 01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a.
The model is already at /opt/carrot-jetlink/cache/nexo-n16-download/carrot-n16.onnx on the user's Jetson; this tool does not download/copy it.

## Why the old vendor does not run N16 as shipped

The pinned Carrot Jetlink builder exists under the protected release's
carrot-server/third_party/jetlink directory. Its older onnx_patch.py has
patch_uint8_inputs but not patch_stateful_images. N16's image queue includes
state_img_q, Slice, Concat, and next_state_img_q; TensorRT 10.3 rejects the
raw UINT8 Slice. User device results on 2026-11-20 proved that translating
these image queue tensors to FP16 makes the TensorRT ONNX parser accept all
11 inputs, 6 outputs, and 0 parser errors. **Parser success is not GPU build success.**

The newer ajouatom/openpilot carrot-wip
third_party/jetlink/jetlink/onnx_patch.py contains patch_stateful_images and the
newer builder invokes it. This trial ports ONLY that already-published
byte-preserving queue conversion to an **isolated worker process**. It does not
modify the installed pinned Carrot package. Runtime monkeypatching the old
builder's patch function and image-input dispatch is strictly scoped to
this one disposable child process.

## Runtime safety

- Explicit operator launch only; nothing runs on install.
- From Jetson, requires verified strict-SSH Comma4
  comma@192.168.100.127, IsOffroad=1, IsOnroad=0, and external AI OFF.
- Existing protected Display-Only NCM mode must remain true.
- Requires >=4 GiB free DATA and >=5 GiB MemAvailable at start; monitor
  stops trial below 1.5 GiB free DATA or 2 GiB MemAvailable.
  It does **not** delete old models, engines, rollback data or any release.
- Exclusive preparation lock, continuous actual Comma4 state monitoring,
  process isolation, bounded 900 second default, cleanup of trial artifacts.
- No changes to production current symlink, modeld, CAN, HUD, runtime
  services, existing TensorRT engines, last-loaded.json or settings.
- No source overwrite. Temporary engine is deliberately **not published**
  into any persistent/live Jetlink cache.

## Invocation after the tested update is installed

From Comma4's terminal (with user already verified Offroad):

    ssh -T jetlink@192.168.100.250 '
      cd /opt/carrot-jetlink/current &&
      /opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
        --comma-ssh comma@192.168.100.127 --samples 3
    '

If the reverse USB SSH guard from Jetson to Comma4 is not provisioned and
verified, the trial **fails closed** before building. Do not bypass
StrictHostKeyChecking or use Onroad/P-only in place of Offroad.

Read-only result (on Jetson):

    cat /opt/carrot-jetlink/cache/nexo-n16-trial-result.json

The result has state=passed and gpu_inference_verified=true only if the
actual TensorRT engine was built, loaded, invoked several times, checked for
exact N16 IO shapes and finite outputs, and then destroyed. This is synthetic
zero-input testing with one constant image value, not real camera input,
model numerical parity, useful driving accuracy, actual HUD projection or
NEXO control approval. Errors must be investigated; do not retry repeatedly
or delete forensic/protected data.

## 2026-11-21 on-device TensorRT OOM and bounded-memory experiment

The first GPU build reached TensorRT tactic selection but did not produce an
engine: `NvMapMemAllocInternalTagged ... error 12`, `Cuda Runtime (out of memory)`,
and `TensorRT returned no engine`. Report shows `gpu_inference_verified=false`
and `artifacts_deleted=true`; production controls and HUD were untouched.
Subsequent read-only samples showed 6.3 GiB MemAvailable at idle, only ~25 MiB
of 3.7 GiB swap used, and `tegrastats` GR3D_FREQ=0%. Idle free memory is NOT
evidence that the TensorRT peak build requirement can fit the 8 GB shared RAM.
The kernel journal did not show a Linux OOM-killed process, which is consistent
with a CUDA allocator failure rather than proof of total memory health.

A new explicit **bounded experiment profile** can restrict the *TensorRT tactic
workspace* to 256 MiB and use builder optimization level 0. These bounds do NOT
limit total TensorRT build memory, model constants or GPU allocations. There is
no automatic retry, swap modification, GPU clock/power override or production
service stop. The parent aborts if Comma leaves Offroad or MemAvailable drops
below 2 GiB (best effort, checked every few seconds). Device may still run out
of GPU memory despite this setting. Do not run repeatedly if the same
allocation failure recurs; collect diagnostic output instead.

From the Comma terminal AFTER the protected display-only update has installed
this version AND the vehicle is parked with `IsOffroad=1`, `IsOnroad=0`:

    ssh -T jetlink@192.168.100.250 '
      cd /opt/carrot-jetlink/current &&
      /opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
        --comma-ssh comma@192.168.100.127 \
        --identity-file /home/jetlink/.ssh/nexo_comma_guard_ed25519 \
        --samples 2 --workspace-mib 256 --optimization-level 0
    '

This tries exactly one isolated engine build, does not auto-publish engines or
activate N16 output, and only uses synthetic inference after a successful
engine build. The result records workspace, optimization level and minimum
observed MemAvailable. A failed attempt is a reason to reassess hardware RAM
limits, not to relax safety controls or delete live models.
