# Carrot N16: offline synthetic CPU numerical comparison

The user's protected NEXO Jetson has already successfully exported two
verified ONNX halves of the pinned official Carrot N16 model (cut node 364
of 1193; left 411,291,511 B; right 403,473,946 B; 8 bridge tensors).
Structural verification succeeds, but that is NOT yet numerical parity.

This commit adds `--partition-parity-only` to the existing guarded
`nexo_n16_trt_trial`. It does **not** generate TensorRT plans, use GPU,
read camera images, draw HUD objects, publish production models, or send CAN.

A fresh CPU-only ONNX preparation copies the exact same Carrot N16 source
and stateful-image patches used for partition extraction into a disposable
staging directory. It then executes ONNX Runtime CPU reference, left half,
and right half **in separate processes** on two deterministic synthetic
input sets. Original inputs are reused unchanged; left intermediate tensors
are explicitly fed into the right session. Both the full output contract
and the raw Carrot N16 lane, road-edge, lead and plan slices are compared
at an engineering screening tolerance of atol=0.005 and rtol=0.005.
The model itself and both previously extracted halves remain untouched.

The existing CommaGuard and engine-preparation lock are mandatory.
The full trial monitors all nested workers, checks real offroad conditions
repeatedly, starts only with >=5 GiB available RAM and >=3 GiB free DATA,
terminates below 2 GiB available memory or 1.5 GiB free DATA, and deletes
only its new disposable stage. It stores:
`cache/nexo-n16-partition-cpu-parity-result.json` and
`cache/nexo-n16-partition-cpu-parity-memory.json`.

If ONNX Runtime CPU rejects a vendor operator or the full graph exceeds
the 8 GiB board's available memory or time limit, the result is FAILED.
Such a failure does not invalidate the preceding successful structural
split and must not be interpreted as numerical success.

After the tested release is installed, invoke ONCE from the Comma4 shell
only with complete IsOffroad=1, IsOnroad=0 and external AI OFF:

```sh
ssh -T jetlink@192.168.100.250 '
cd /opt/carrot-jetlink/current &&
cat SOURCE_COMMIT &&
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
 --comma-ssh comma@192.168.100.127 \
 --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
 --partition-parity-only
'
```

Only `state=cpu_parity_passed` plus `cpu_numerical_parity_verified=true`
would establish synthetic CPU numerical parity for these two test cases.
It does NOT demonstrate original real-world camera accuracy, GPU fit,
GPU speed, lane projection on the 2019 NEXO HUD, or driving approval.
The existing display-only and read-only vehicle guards remain mandatory.

Because the two exported ONNX files occupy roughly 815 MB, ensure free
DATA is sufficient before installing the next HUD software release.
Do not delete official N16 models, rollback metadata, or protected caches.
