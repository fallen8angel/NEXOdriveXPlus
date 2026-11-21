# Carrot N16: isolated two-part ONNX export for the 2019 NEXO

The 8 GiB Jetson failed to build a full TensorRT N16 engine even with
separate ONNX preprocessing, 256 MiB WORKSPACE, 512 MiB TACTIC_DRAM,
optimization level 0 and one build thread. The **CPU-only planner**, running
on the real Jetson with original SHA-pinned N16, reported
`state=candidate_found` and the best `cut_node_index=364`.
The left/right estimated initializer weights were 410,051,336 and
401,222,888 bytes, with 1,319,542 bytes of inter-part boundary tensors.

## What this version implements

- A new **explicit** `--partition-extract-only` trial mode. It uses the
  unchanged SHA256-verified official Carrot N16 ONNX, removes only the known
  tinygrad `Contiguous` layout hint and applies the already-reviewed
  stateful image-queue UINT8->FP16 patch in CPU-only memory. It never invokes
  TensorRT or CUDA and does not enable vehicle control or HUD rendering.
- Two **sequential short-lived child processes** independently load,
  patch and extract one half each. A process exits before the other loads
  the ~815 MB original, to reduce overlapping host memory use.
- Each part keeps original node arithmetic and the initializers it
  actually uses. The matching intermediate tensors, original inputs,
  states and output signatures are preserved as explicit ONNX graph IO.
- Both ONNX graphs are checked by ONNX checker, hashed, reloaded and checked
  again. The concatenated serialized node stream must match the freshly
  patched original exactly; an altered cut or corrupted file fails closed.
- The guarded parent stores both tested graphs **only after** final Offroad
  verification, in an *experiment-only* directory on the DATA partition:
  `/opt/carrot-jetlink/cache/nexo-n16-partition-export-01effc679eb4/`
  Files: `left.onnx`, `right.onnx`, `left.json`, `right.json`,
  `verification.json`. Existing exports are never overwritten or removed.
- Result and timeline receipts are separate from earlier GPU experiments:
  `cache/nexo-n16-partition-extract-result.json` and
  `cache/nexo-n16-partition-extract-memory.json`.
  Failed attempts delete only their newly created disposable stage.

## Requirements

This is *not* numerical parity or engine readiness for the original N16.
It does not prove that either half fits on the Jetson GPU, matches CPU
reference predictions, meets realtime latency or renders actual HUD pixels.
CPU tests demonstrate numerical parity on a **tiny synthetic test graph**;
do not interpret them as actual Carrot N16 parity.

The existing guards remain: strict reverse SSH verification to Comma4,
IsOffroad=1, IsOnroad=0, external AI OFF, protected display-only NCM mode,
pinned source SHA, minimum initial 5 GiB MemAvailable / 4 GiB DATA free,
2 GiB runtime memory reserve, 1.5 GiB remaining DATA and bounded timeout.
Do not run while driving, do not treat P-gear as Offroad, do not delete
protected caches, change swap/power mode or bypass a failed guard.

## One-time command after the tested HUD release is installed

Confirm that the installed `SOURCE_COMMIT` is the commit containing this
feature. On Comma4 with full Offroad and the previously approved SSH key:

```sh
ssh -T jetlink@192.168.100.250 '
cd /opt/carrot-jetlink/current &&
cat SOURCE_COMMIT &&
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
  --comma-ssh comma@192.168.100.127 \
  --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
  --partition-extract-only
'
```

Look for `state=models_extracted`,
`structural_verification_passed=true`,
`node_stream_recombined_exact=true`, and separate `left_model_path` /
`right_model_path`. If it fails, preserve output and do not retry blindly.

Next development gates after a successful export are (1) independently
measured **actual N16 numerical parity** against an original ONNX reference,
(2) separate TensorRT engine build/load/synthetic inference for each half
subject to strict existing resource guards, (3) calibrated NEXO
camera-to-external-HUD output, without driving control.
