# N16 cut-364 numerical error localization — 2019 Hyundai NEXO

## Motivation and actual status

The pinned original Carrot N16 ONNX (SHA256
`01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`)
has been split into two structurally verified ONNX halves at node 364/1193.
The verified split exports have eight cut tensors.

On the user's 2026-11-21 Jetson, ONNX Runtime 1.23.2 CPU execution of
**two deterministic synthetic inputs** completed, but numerical parity
**FAILED**. Raw 18,580-element N16 output mismatches were 392 and 143
elements respectively at atol=0.005, rtol=0.005. Lead, lead probability,
road-edge and lane probability differences were observed. Neither TensorRT
nor physical HUD nor vehicle control has been verified.

This change adds a **diagnostic** mode. It does NOT silently relax the
tolerance, change ONNX arithmetic, enable GPU TensorRT or publish a
model. Production driving and all HUD AI overlays stay disabled.

## Diagnostic design

`--partition-boundary-diagnose-only` uses the existing Comma4 Offroad and
AI-off guardian, pinned ONNX SHA/size, USB-NCM release lock, sample memory
watchdog and DATA disk floor. It rejects simultaneous build flags.

With CPUExecutionProvider only and fresh **isolated workers**, it runs:

1. Exact original patched N16 ONNX with synthetic inputs -> reference
   final outputs and shared inputs.
2. A **disposable copy** of the same original model with eight verified
   cut tensors appended as graph outputs. No operator, initializer or
   function changes are permitted. Re-run the original outputs and record
   all eight original intermediate tensor values.
3. The previously verified left ONNX -> eight left intermediate tensors.
4. The previously verified right ONNX receiving real left intermediates
   -> combined split chain outputs.
5. The same right ONNX receiving the **original's own intermediate tensors**
   -> isolates downstream numerical or boundary input differences.

The instrumented reference's original final outputs are independently
compared with the uninstrumented reference first; if they shift, the
result is explicitly **inconclusive**, not misleadingly attributed to
the split.

For each of the two synthetic cases all eight intermediate tensor
absolute errors, tolerance failures and RMSE are recorded, as well as
full original vs combined outputs and downstream-only original-boundary
outputs. The same original tolerance (atol=rtol=0.005) is used; errors
are never suppressed.

### Expected result fields

- `state=boundary_diagnosis_complete`: the comparisons completed; it
  **does not mean parity passed**.
- `reference_probe_unchanged=true`: exposing the eight graph outputs
  did not change final original synthetic predictions.
- `left_boundary_agrees`: original cut intermediates vs left outputs.
- `right_using_true_boundary_agrees`: original vs right half with
  the original's own cut inputs.
- `full_split_chain_agrees`: full original vs left+right chain.
- `candidate_location`: diagnostic **hypothesis**, not a proved fix.
- `numerical_parity_verified=false`, `gpu_inference_verified=false`,
  `vehicle_control_enabled=false`: held false throughout this mode.

The diagnostic report and independent bounded memory timeline are stored
at:

- `cache/nexo-n16-partition-boundary-diagnosis-result.json`
- `cache/nexo-n16-partition-boundary-diagnosis-memory.json`

Only concise read-only summaries are also included in the Jetson unified
ZIP (`remote/18_n16_boundary_diagnosis_status.json`,
`15_n16_boundary_diagnosis_summary.json`,
`15_n16_boundary_diagnosis_summary.txt`). Downloading the ZIP NEVER
runs CPU inference.

## Once-only test after verified HUD update

Only when the newly tagged release is installed on the Jetson, the
Comma4 is fully OFFROAD (`IsOffroad=1`, `IsOnroad=0`), external
AI control is OFF, and no Jetson updater/AI engine builder is active:

```sh
ssh -T jetlink@192.168.100.250 '
cd /opt/carrot-jetlink/current &&
cat SOURCE_COMMIT &&
df -h /opt/carrot-jetlink &&
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
 --comma-ssh comma@192.168.100.127 \
 --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
 --partition-boundary-diagnose-only
'
```

Preflight requires at least 3 GiB available on DATA and 5 GiB available
RAM. A second temporary copy of the original ~815 MB ONNX is created
for this offline diagnostic and deleted after use. The existing two
verified halves and all production model/engine/rollback files are
preserved. The protected parent refuses/aborts the experiment below
1.5 GiB DATA or 2 GiB MemAvailable, on elapsed timeout, if offroad
changes, or if the installed release changes. Do not re-run after a
failure before investigating its first receipt.

## Decision gate

Only verified synthetic CPU evidence may justify investigating a
precision-preserving split export or GPU engine strategy. The next
actual work is to interpret where error begins, fix that cause without
raising the tolerance, then repeat the **original CPU parity check**.
GPU inference, NEXO camera alignment, rendering projection and the
physical HUD remain separate gated steps.
