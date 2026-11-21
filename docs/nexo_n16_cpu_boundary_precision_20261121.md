# N16 CPU split accuracy: boundary materialization, not new weights

## Provenance and scope

This extends PR #79 rather than replacing its cut-aware diagnosis. Public heads
checked on 2026-11-21: NEXO `59cc9f154adc27cface2eba17a84fd94ea61648d`,
CarrotPilot `77dd33357a73c78745f7c27c9656e50da3c8ec40`,
CarrotAssist/countpia-a `ad0943c0fdfdd7a08762f73f5b88f30090e129b8`, and
Carrot Jetson `d0dc96fa32410403dfb2fefff3e2909c11ec109b`.
The Pilot and Assist N16 specs match the existing NEXO spec byte for byte.
The official ONNX was downloaded only into this development workspace and
verified as 814591107 bytes, SHA256
`01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`.
No vehicle download, installation, activation or engine publication occurred.

## Reproduced cause

ORT 1.23.2 CPUExecutionProvider, even with `ORT_DISABLE_ALL`, inserts
`InsertedPrecisionFreeCast_*` and evaluates FP16 kernels internally in FP32.
The whole CPU graph can carry FP32 intermediates across many FP16-declared
operators. Exposing one of those intermediates as an FP16 graph output adds
a rounding point. Unchanged ONNX node streams, Local Functions and initializers
therefore do **not** prove unchanged CPU arithmetic across a split.

A real MatMul/Add/MatMul FP16 unit graph reproduces this without N16 weights.
The N16 four-way experiment separately ran original, instrumented original,
left, right-with-original-boundary and connected halves for cuts 360/361/364.
Native cut 360 produced final-output absolute differences up to 0.0625 and
`next_state_feat_q` differences up to 0.0078125 in the two deterministic cases.
Native cut 361 also exceeded 0.005; moving the cut is not the fix.

For cut 360, the first observed precision-losing **boundary producer** is
node index 352, `node_add_27`, output `add_27`. Comparing its FP32 transport
against the native FP16 boundary showed maximum differences 0.123077392578125
and 0.20916748046875 in cases 0/1. Node index 359,
`node_MatMul_407`, output `val_471`, also loses precision at materialization.
This locates the observable divergence, not an incorrect Add/MatMul weight.
The seven `_to_copy*` boundary tensors showed zero difference in those cases.
Instrumented-original output changes remain explicitly inconclusive for the
original eight/nine-boundary diagnostic; they are never silently accepted.

The prior `atol=0.005, rtol=0.005` policy could accept errors above 0.005 on
large outputs. The shared policy is now **absolute 0.005, rtol=0**. No failed
output is excluded and no tolerance is increased.

## Implemented CPU-only correction

`nexo_n16_cpu_precision.py` derives halves using the existing extractor, then
adds an explicit Cast-to-FP32 at each left boundary and Cast-back-to-FP16 at
the matching right input. Weights, Local Functions, original operator order,
and the public N16 input/output contract remain unchanged. FP16 -> FP32 ->
FP16 transports representable FP16 values exactly. CPU ORT can retain its
internal precision through these bridges instead of rounding at session IO.
Both tested cut-360 synthetic cases had **zero error on every public output**.
This is a CPU execution correction; TensorRT has different kernels and must
be validated independently. No transport graph is written to an engine cache.

Cut 361 also needed the right-side Cast moved after its first single-consumer
`Transpose` (`node_permute_63`), otherwise that native FP16 layout kernel
materialized the rounding before the next arithmetic operation. The corrected
cut 361 produced zero error on all six outputs in both initial cases. This
does not establish 140-frame acceptance for cut 361. Cut 360 has zero such
layout moves; its 140-frame execution graph remains unchanged.
This is not a universal CPU-equivalence rewrite: an additional real-ORT test
retains a different-layout counterexample and verifies its error is rejected.
Every proposed candidate still needs the complete raw-original comparison;
casts and successful structural checks never grant acceptance by themselves.

The 140-frame validator compares the **raw UINT8-input original** against the
adapted halves, with only the already-supported tinygrad Contiguous no-op
removed for ORT compatibility. Its image adapter rejects non-integer,
nonfinite and out-of-0..255 values; it does not round or clip pixels. Each
path feeds back its own five recurrent states without resets or reference
state substitution. All six outputs and all registered output slices are
checked every frame. CPU timing and process RSS are measured and labeled
CPU-only; short runs cannot report 140-frame acceptance.

Actual cloud x86_64 / ORT 1.23.2 result for cut 360: **140/140 independent
recurrent frames, 140 distinct image inputs, all six outputs and all 17 slices
maximum absolute error 0**, initializer integrity and Local Functions preserved.
CPU split latency was mean 1990.13 ms, p50 1971.00 ms, p95 2309.18 ms,
maximum 2503.71 ms; raw-original CPU mean 2437.25 ms. Process high-water RSS
including preparation and the three sessions was 5296611328 bytes (4.93 GiB).
These are cloud CPU observations, not Jetson CPU/GPU measurements.

## Guarded device validation, still required

After normal operator-controlled code deployment, use the existing Comma SSH
identity and a genuine Offroad state (IsOffroad=1, IsOnroad=0, external AI OFF):

```sh
python -m openpilot.tools.jetson.nexo_n16_trt_trial \
  --comma-ssh comma@192.168.100.127 \
  --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
  --partition-stateful-cpu-only --partition-cut 360
```

The parent retains the existing source SHA, Display Only/NCM, release/config
identity, exclusive lock, memory/disk floors, timeout and repeated Offroad
checks. CPU and TensorRT build modes cannot be combined. Three CPU sessions
coexist, so the memory guardian may refuse or stop this on an 8GB Jetson;
do not relax its reserve. The result and RAM timeline are separate
`cache/nexo-n16-stateful-cpu-parity-{result,memory}.json` files, preserving
earlier native split failure/diagnosis reports. The existing unified ZIP's
CPU parity status includes the new result without executing inference.

## Upstream comparison and deferred work

| Upstream implementation | NEXO decision |
| --- | --- |
| Pinned N16 identity, native camera warp and stateful JetLink contract | Reuse identity and contract; validate exact image-byte adaptation. NEXO camera geometry remains unverified. |
| Whole-ONNX TensorRT builder, runtime/GPU-specific engine and timing cache | Preserve; do not substitute CPU validation graphs or overwrite existing engines. Whole ONNX is the preferred GPU fallback if partitioning is unsuitable. |
| Memory-scaled workspace and shared-memory constraints | Retain existing guards and measure CPU RSS; CPU timing is not Jetson GPU timing. |
| Model selection frozen for a boot and explicit offroad library operations | No automatic model switch, delete or activation is introduced. |
| Carrot driving policy, NEXO-incompatible camera/control integration | Do not copy; steering, acceleration, braking, CAN and live modeld are outside this change. |

Cloud CPU evidence, Jetson CPU/GPU evidence, NEXO camera/HUD projection and
vehicle acceptance are distinct. A CPU pass never sets GPU, HUD or control
approval. GPU engine creation remains deferred until its own memory, numeric,
performance and rollback validation can be performed on the actual device.
