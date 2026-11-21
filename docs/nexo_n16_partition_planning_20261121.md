# Carrot N16 on Jetson 8 GiB: safe ONNX graph partition planning

The 2026-11-21 low-memory single-engine run with 256 MiB WORKSPACE,
512 MiB TACTIC_DRAM and optimization level 0 **still failed** at
`tensorrt_engine_build` progress 0.5. The observed worker RSS peak
was 5,128,724,480 bytes and the lowest MemAvailable was 2,099,941,376
bytes. No full-model TensorRT engine or GPU inference was verified.

This change adds a **separate CPU-only partition planning mode**. It
loads the exact SHA256-pinned official Carrot N16 ONNX file, applies the
previously verified Carrot image queue compatibility patch in transient
memory and finds static two-stage ONNX dataflow cuts. It ranks boundaries
using split initializer weight size, shared initializer duplication,
intermediate transfer tensor shapes and estimated transfer bytes.

It refuses cuts with unknown or dynamic tensor dimensions, nested ONNX
control flow, non-topological graphs, excessive interface sizes or badly
unbalanced weight sizes. Each candidate lists its exact boundary tensor
names, dimensions, original external inputs, original outputs and a
cut node index. These are planning heuristics, **not GPU memory
measurements and not numerical parity proof**. No submodel is exported,
no inference engine is built, no actual image is processed and no HUD
graphics or control messages are produced.

The same strict Comma4 Offroad=1, Onroad=0 and JetlinkEnabled=false
guard, pinned model SHA, protected display-only release, memory/disk
reserve, subprocess isolation and cleanup stay active. The planning
result is stored separately in `cache/nexo-n16-partition-plan.json`
and its resource timeline in `cache/nexo-n16-partition-memory.json`.
The failed full-engine receipt remains untouched.

After this commit's protected HUD package **passes CI and is
installed** on the Jetson, invoke from Comma4 in actual Offroad:

```sh
ssh -T jetlink@192.168.100.250 '
cd /opt/carrot-jetlink/current &&
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
 --comma-ssh comma@192.168.100.127 \
 --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
 --partition-plan-only
'
```

Interpret `state=candidate_found` as a feasible *structural idea only*.
Next engineering stage: extract the two subgraphs, validate the original
N16 numerical outputs against the two-subgraph reference and inspect
intermediate bytes before attempting separate GPU builds. Do not rerun
the previous full-model builder flags; do not lower the 2 GiB runtime
memory safety floor, delete rollback caches, or enable driving AI.

ONNX structural extraction guidance: https://onnx.ai/onnx/api/utils.html
