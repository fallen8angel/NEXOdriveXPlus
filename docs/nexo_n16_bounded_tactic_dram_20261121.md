# Carrot N16 experimental TACTIC_DRAM build profile

The 2019 NEXO Jetson 8 GiB previously stopped an isolated N16 TensorRT
engine build at progress 0.5: maximum worker RSS 5,358,632,960 bytes and
minimum MemAvailable 1,979,183,104 bytes, with NvMap allocation error 12.
The old --split-builder --workspace-mib 256 --optimization-level 0 did not fit.

New optional --tactic-dram-mib 512 or 1024 sets TensorRT's TACTIC_DRAM
memory-pool ceiling and single builder thread/zero auxiliary streams.
Unlike WORKSPACE, TACTIC_DRAM caps part of tactic evaluation. Neither
limit guarantees total process or GPU memory stays below a particular size.
A disposable experiment worker also sets CUDA_MODULE_LOADING=LAZY unless
preconfigured. Uses the exact source/hashes and the pinned Carrot builder;
the original module is restored after each build call.

No automatic builds, retries or production engine publication are added.
Preserve strict Jetson-to-Comma SSH, full Offroad=1/Onroad=0, external driving
AI disabled, display-only mode, initial minimum 5 GiB RAM/4 GiB free DATA,
ongoing 2 GiB RAM/1.5 GiB disk floor, timeout, and temporary file cleanup.
Never enable driving AI, vehicle CAN, or physical HUD automatically.

Once the HUD ZIP for this commit is fully verified and installed on Jetson,
confirm cat /opt/carrot-jetlink/current/SOURCE_COMMIT, then (only offroad):

  ssh -T jetlink@192.168.100.250 '
    cd /opt/carrot-jetlink/current &&
    /opt/carrot-jetlink/venv/bin/python -m openpilot.tools.jetson.nexo_n16_trt_trial
      --comma-ssh comma@192.168.100.127
      --identity-file /opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519
      --split-builder --workspace-mib 256 --optimization-level 0
      --tactic-dram-mib 512 --samples 2
  '

Command above is schematic; use a single continued shell command. Run once.
Even if a synthetic GPU inference passes, numerical accuracy, actual NEXO
camera geometry, physical HUD output and vehicle-control approval remain false.

NVIDIA documentation: https://docs.nvidia.com/deeplearning/tensorrt/latest/architecture/how-trt-works.html
