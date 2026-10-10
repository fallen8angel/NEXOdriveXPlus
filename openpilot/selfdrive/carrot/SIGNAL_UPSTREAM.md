# NEXO optional signal experiment

Source: ajouatom/openpilot `carrot-wip`
[`c6925561478f8fc84e50fce4373c87b0dd075d4a`](https://github.com/ajouatom/openpilot/commit/c6925561478f8fc84e50fce4373c87b0dd075d4a),
selectively adapted on 2026-10-11 at the user's request.

Includes the classical RGB observer/tracker, bounded observation transport,
optional red-stop assistance, CPU comparison workers, offline replay/training
tools and focused tests. Upstream vehicle trial documents, private recordings,
AGENTS memory and unrelated Hyundai CAN-FD changes are not imported. No ONNX
model, failed training candidate, installed flag or device runtime is deployed.

## Integration

NEXO retains its existing CarrotPlanner, driving modes, comfort braking, 20-degree
moving-stop entry guard, lead/turn/cooldown gates, driving ONNX and model output
layout. Signal assistance uses the existing NEXO filtered stop distance. Green
removes the assistance constraint only; normal NEXO logic decides departure.
Unknown cannot release a latched experimental stop. Gas, disengagement, invalid
inputs or leaving Drive reset assistance; gas prevents reacquisition for 10s.
The assistance requires fresh/seen/valid/alive carState, modelV2, radarState,
selfdriveState and carControl, valid CAN and active longitudinal control.

The worker is separately managed onroad, with normal priority, little-core
affinity and bounded CPU duty. Tracking reads the road-camera NV12 buffer and
does not use the Jetson transport. Policy comparison attaches only to
`modeld_local.py`; its samples never become modelV2 predictions. Native Jetlink
remote inference, SHA contract, CAN publishers and direct/Jetson HUD modes are
unchanged. No broad upstream modeld/planner replacement is performed.

All flags are absent/OFF by default. The imported interfaces are:

- `/data/signal-color-shadow/enabled`: camera observer, next onroad start.
- `/data/signal-color-shadow/tracking_enabled`: classical tracking mode within
  that observer. It needs existing OpenCV/NumPy, not a learned signal ONNX.
- `/data/signal-color-shadow/assist_enabled`: separate control opt-in, latched
  at plannerd startup. Turning it OFF suppresses new assistance within 0.5s;
  turning it ON requires plannerd restart. Existing NEXO stop-state handling
  continues after disabling. No flag is created by this code update.
- `/data/signal-model-shadow/enabled`: optional policy comparison, next local
  model startup, requiring separately supplied artifacts and matching hashes.

The color-pool comparison mode also requires an externally supplied, verified
ONNX manifest. The source commit does not include those learned artifacts.
Offline training requires its own PC environment (PyTorch/ONNX); its outputs
are not installed by any manager or updater. The offline observer requirements
match the versions used for NEXO desktop tests.

## Limits and validation

Forward-image selection does not establish the applicable lane, arrows or
stop-line distance. Moving-red assistance uses the original model distance and
does not invent a distance from color. Enabled operation consumes additional
CPU and can change stopping/holding decisions. Observation loss retains a
latched stop until an explicit exit. This is experimental, not validated NEXO
closed-loop braking or guaranteed traffic-signal recognition.

Desktop checks cover observer/tracker, transport, red hold, same-track green,
driver override, NEXO entry gates, default OFF, manager registration and original
model output preservation. A deterministic differential check against the
pre-change NEXO planner covered 4,800 steps across driving/detection modes,
navigation, speed/lead changes, pedal inputs, blinkers, steering and soft hold;
OFF outputs/events matched on every step. This is synthetic state-machine
validation, not replay of the user's route or a device/vehicle test.

Final focused suite: 340 passed, 1 Linux-native import test skipped on Windows.
All 24 changed/added Python files passed syntax checks; all 21 new Python files
passed repository Ruff rules. Existing unrelated lint findings in the original
planner/local model are not reformatted. The three existing Jetlink shell
scripts passed syntax checks. Full Linux build, camera/CPU timing on Comma,
trained-artifact generation and vehicle braking are not validated here.

## Boot compatibility correction

The first NEXO integration used upstream's `PythonProcess(..., spawn=True)`
without the matching upstream manager API. NEXO's constructor rejects that
keyword while evaluating the process registry, preventing manager startup even
with every signal flag OFF. The earlier registration test used a permissive
constructor replacement and missed the incompatibility.

Signal observation now uses NEXO's existing `NativeProcess` exec launcher to
start a fresh Python interpreter at `sys.executable`. The base PythonProcess API
and other process launchers are unchanged. A registry test executes every
production registration with the real NEXO constructor definitions; it reproduces
the pre-fix TypeError and succeeds after the correction. Signal opt-in gates
and worker failure handling remain unchanged.

Correction validation: 344 focused tests passed, 1 Linux-native test skipped.
The registry and exec-launch tests use the actual production class/function
definitions with native imports isolated. Four changed/added Python files passed
syntax and Ruff checks. A physical Comma restart remains to be verified.
