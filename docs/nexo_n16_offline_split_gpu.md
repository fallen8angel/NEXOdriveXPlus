# N16 cut-360 offline TensorRT validation

The Jetson operator reported 140 recurrent CPU frames with zero error at cut 360 on adca07e5. The reference process peaked at about 3.73 GB and the split process at about 1.22 GB. They ran at different times. Without the original memory timeline the minimum free memory during each phase and the GPU capacity available at load time are unknown.

The CPU receipt identifies the ONNX Runtime execution graph of the whole pinned N16. On the reviewed graph seven boundary tensors are FP16 and the producer outputs for `add_27` and `val_471` are FP32. The trial reproduces the graph and all nine edges, checks every original initializer and matches the first-frame input and public outputs before starting a builder. It regenerates the CPU reference from the original UINT8 model. Each path carries its own state over 140 deterministic synthetic inputs. The comparison includes all six outputs, 17 output slices and the first-frame boundary. The absolute tolerance is 0.005.

The upstream N16 Nav catalog reviewed at ajouatom/openpilot 6a9225b5 identifies the same ONNX payload as the local N16 spec. The Jetlink runtime at ajouatom/carrot-jetson d0dc96fa preallocates pinned host and device buffers and synchronizes an execution stream. This trial reuses that executor. Only the byte-valued image queue of the temporary left graph is adapted to FP16. Blanket FP16 lowering and optional CUDA graph capture have no device parity evidence for this cut. CarrotAssist ad0943c0 was also reviewed. The trial uses no alternative weights, eGPU artifacts or navigation-to-control mapping.

## Run from the comma4 shell

Use the normal verified package updater first. Keep the Jetson powered, with the comma4 reporting Offroad and external AI OFF. The trial needs the local model, a passing cut-360 CPU receipt, trusted SSH in both directions, 4 GiB available storage and 5 GiB available RAM at startup. The USB-NCM connection supplies safety checks throughout the run. Internet access and a live camera are unnecessary.

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes jetlink@192.168.100.250 '
  cd /opt/carrot-jetlink/current &&
  cat SOURCE_COMMIT &&
  /opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial \
    --comma-ssh comma@192.168.100.127 \
    --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519 \
    --partition-gpu-parity --partition-cut 360 --timeout-s 7200
'
```

The default total deadline is 3600 seconds. The example selects the maximum 7200 seconds for the 8 GB board. The CPU reference, boundary probe and each engine build run in successive processes. Each build has 900 seconds, 256 MiB workspace, 512 MiB tactic DRAM, one builder thread and zero auxiliary streams. Both engines must fit together in the replay process. The trial retains the 2 GiB system RAM and 1.5 GiB disk reserve during execution. Build limits cover specific TensorRT pools. Device-wide high-water allocation can exceed those pool limits.

## Read the results

On the Jetson, read `/opt/carrot-jetlink/cache/nexo-n16-split-gpu-parity-result.json` and `/opt/carrot-jetlink/cache/nexo-n16-split-gpu-parity-memory.json`. The integrated diagnostic ZIP includes the summary in `remote/17_n16_cpu_parity_status.json`. The CPU acceptance receipt and production engine cache are preserved. Test graphs, journal and engines belong to a temporary directory removed at the end.

- `gpu_parity_passed` requires all 140 frames within tolerance. Inspect `failed_outputs` and per-output maxima for a completed mismatch. For an incomplete trial read `phase`, `error`, `diagnosis` and the TensorRT warnings. A completed synthetic run grants no operational approval.
- `left_ms` and `right_ms` measure each synchronized call including host input copies and transfers. The paired FPS uses the summed call times. `split_frame_ms` includes input preparation, handoff and output copies. These scopes exclude the time spent verifying public results and have no relation to live camera or display FPS.
- CUDA free and total bytes are sampled at phase boundaries and per replay frame. They cover the shared device, can miss transient peaks, and are recorded as unavailable if CUDA cannot supply them. The host timeline independently tracks process RSS and system memory.
- HUD diagnostics consume recorded summaries. The trial neither sends live predictions nor selects a service or a production plan. The protected updater retains its rollback procedure.
- Release switching leaves the operator's SSH files and persistent trial identity in place. Strict host-key checks apply in both directions in the example. On a device with a volatile home, verify the reverse host fingerprint after reboot. The protected boot recovery for the approved comma-to-Jetson public key does not establish Jetson-to-comma host trust.

## Diagnosing a safety-check interruption

Preflight inventories the local SSH credentials and remote public-key fingerprints. Each periodic and final GPU-trial check validates the effective SSH destination, explicit identity permissions, direct USB-NCM route and carrier, then opens a fresh strict public-key SSH connection to read the comma's two Offroad/Onroad Params and external-AI marker. The periodic command skips Python site initialization and public-key enumeration. A missing response, timeout, changed key or unsafe state stops the trial on that check. The existing connect and remote-command limits are 3 and 7 seconds.

For an interruption, compare the trial's `phase` and one-based `frame_progress` with `ssh_guard.stage`, `remote_step`, `elapsed_ms` and the separate SSH preflight receipt. A remote marker identifies the last step witnessed. A timeout before any marker can include connection, authentication or Python startup. Local config and NCM checks have separate stages. The CPU reference and probe workers run with lower scheduling priority. The result's `cpu_runtime` identifies the actual imported ORT package, version and CPU provider even when the reference is interrupted after startup.

The passing local CPU receipt remains a prerequisite. It contains summary errors and a graph contract. The previous run's temporary per-frame output journal was removed. A summary or an interrupted journal cannot supply a 140-frame oracle. Each GPU trial generates a fresh journal and verifies the original model, ORT version, resolved boundary, input digests and recurrent sequence. Cross-run journal caching would also need provenance for the reference implementation, all 140 input/output records and their digest, atomic publication after full validation, and enough remaining space for both engine builds.
