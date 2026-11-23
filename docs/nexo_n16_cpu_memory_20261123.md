# N16 CPU validation on an 8GB Jetson: non-overlapping reference and split sessions

## Problem and source comparison

Baseline NEXO commit: `d8df108a141ac97f0ce6ab79bbbeb0dd41939c3a`.
The reported device run stopped at frame 2 with worker RSS 5268328448 bytes,
MemAvailable 2048389120 bytes, and the existing 2GiB memory reserve guard.
The previous implementation retained a full N16 reference session plus both
halves in one process. Disabling ORT memory arenas and collecting Python
graphs did not release those live sessions or all native allocator memory.
Earlier cloud execution also measured approximately 5.3GB process high-water RSS.

Public heads checked: CarrotPilot `8391409993343755de87040ab12cb71d0815c9aa`,
CarrotAssist `ad0943c0fdfdd7a08762f73f5b88f30090e129b8`, and Carrot Jetson
`d0dc96fa32410403dfb2fefff3e2909c11ec109b`. The recent Pilot changes are ATC
settings/admission changes, not a different N16 model or a CPU parity fix.
The existing model spec and image patch remain applicable. The upstream TRT
builder scales workspace from available RAM, uses temporary patched files,
writes an engine atomically, and carries model metadata with cached engines
so loading needs no ONNX parser. Those patterns support bounding lifetimes and
preserving existing caches. They do not solve three simultaneous CPU sessions.
We do not copy driving policy, change TRT options or introduce another model.

## Implementation

1. A spawned **reference-only** process creates one raw UINT8 N16 CPU session,
   runs the complete 140-frame sequence with its own state, and writes a bounded
   private reference journal.
2. The process exits and is joined before the next process starts. Process exit
   releases ORT, protobuf and native allocator memory; Python GC is not the
   mechanism for reclaiming the original session between phases.
3. A **split-only** process creates the two existing precision-corrected halves.
   It regenerates exactly the same inputs, maintains its own candidate state,
   and compares every output against one reference record at a time.

The journal uses Python's standard-library streaming XZ/LZMA2 with a 16MiB
dictionary, CRC64, per-record NPZ CRC and bounded lengths. This compresses
overlap in image/feature history queues across frames without lossy conversion
or an additional installed package. The compressed journal is limited to
256MiB, each raw record to 64MiB. Every reference frame records its index and
SHA256 of **all inputs, including that path's recurrent state**. Replay checks
those hashes, source SHA, IO metadata, frame count and the final stream EOF.
Missing, altered, extra or truncated records cannot pass. No input or output
tensor is excluded. The image adapter and exact 0.005 absolute / zero relative
tolerance are unchanged.

Only temporary reference outputs are written; ONNX weights and engine files
are never rewritten. The original source remains SHA-pinned to
`01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`.
All six outputs and 17 output slices are still compared through the existing
comparison routine. Real reference inference latency is recorded separately
from journal replay/decoding time.

## Memory evidence and failure behavior

Reports record RSS/peak before loading, after graph loading, with each session
live, after graph release and periodically through frames. They include both
worker PIDs, child high-water RSS, sampled control/child process-tree RSS,
maximum input/intermediate tensor bytes and reference-journal size. These are
host-memory observations, not exclusive ORT allocations or CUDA memory peaks.
The controller and its known active CPU worker are also sampled directly by
PID. The managed development kernel omits task `children` files, so relying
only on recursive procfs discovery would incorrectly report just the small
controller. The direct sum avoids both that omission and double-counting on
normal Linux. It supplements, and never replaces, the global MemAvailable guard.
Partial stage observations are copied to the outer worker status so a memory
guard abort preserves diagnostic evidence before temporary files are removed.
The unified diagnostic's existing CPU status includes these fields read-only.

The parent continues checking actual Comma Offroad/external-AI-OFF, release and
configuration identity, the exclusive preparation lock, 2GiB MemAvailable
reserve, 1.5GiB storage floor, initial storage/RAM checks and the existing time
limit. It monitors the complete worker tree. The new worker lifecycle also
terminates and joins a child when a callback/worker fails. Only this run's
temporary journal is cleaned; prior experiments, engines, models, services and
the selected runtime are preserved. There is no automatic retry or activation.

## Verification and device follow-up

Automated tests exercise real record/replay for 140 recurrent frames, changed
inputs/state, missing/truncated records, low disk, real subprocess exit/RSS and
memory-guard cancellation with existing model/engine/release preservation.
Full official-model cloud measurements and latest CI results are recorded in
the pull request. Cloud CPU evidence does not establish acceptance on Jetson.

After installing the normally verified package through the existing update
process, preserve Jetson power and verify genuine Offroad plus external AI OFF.
From a PC with the existing approved SSH configuration:

```sh
ssh -J comma@172.20.10.11 jetlink@192.168.100.250
cd /opt/carrot-jetlink/current
cat SOURCE_COMMIT
/opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
  --comma-ssh comma@192.168.100.127 \
  --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
  --partition-stateful-cpu-only --partition-cut 360
```

Inspect the result and memory timeline before separately testing cut 361:

```sh
cat /opt/carrot-jetlink/cache/nexo-n16-stateful-cpu-parity-result.json
cat /opt/carrot-jetlink/cache/nexo-n16-stateful-cpu-parity-memory.json
```

The same command with `--partition-cut 361` performs another full validation.
Never lower memory/disk floors or bypass the Offroad/SSH check to finish it.
If device CPU time exceeds the existing time limit, report that blocker rather
than claiming accuracy success. TensorRT/GPU validation and physical HUD/NEXO
acceptance remain separate steps. No driving-control activation is included.
