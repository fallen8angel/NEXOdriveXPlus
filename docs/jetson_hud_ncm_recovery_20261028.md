# HUD/NCM recovery (2026-10-28)

Base: `4eabb4a77f14842c38586a36f27dad05d9b47f1c`.
Branch: `fix/jetson-hud-ncm-recovery-20261028`. No automatic merge.

## Findings and behavior

- The protected boot screen runs a separate immutable `/usr/local/lib` copy.
  Updating only the DATA HUD cannot update that screen's ownership protocol or
  transform. The NCM activation helper now stops that screen before installing
  RAM-only service drop-ins. Both DATA entry points run as `jetlink` and share
  `/run/carrot-jetlink-display/owner.lock`. The HUD request stays alive for the
  entire ownership period; boot closes USB before releasing its lease. Duplicate
  HUD request writers are serialized, stale PID/boot identities are rejected,
  and a HUD waiting for ownership times out after eight seconds.
- The H264 writer thread wraps `SnapshotUnavailable` in `RuntimeError`; cluster
  main then normally converts that to `SystemExit`. This bypassed the old HUD
  exception handler and charged data expiry to the USB failure budget. NEXO now
  calls cluster main with `exit_on_error=False`, follows the exception chain,
  records a bounded failure category, and returns exit 75 for input expiry.
  `RemoteSubMaster.update` no longer waits up to two seconds on each read. The
  receiver lease stays at 0.5 seconds. Expired data never authorizes a panel write;
  renderer teardown blanks the panel before releasing ownership to boot.
- A capture taking 0.5 seconds or more submits a black RGBA frame instead of old
  speed/vehicle content. This preserves the running encoder through slow initial
  shader/font setup. `render_expired` and `render_ms` distinguish this from an
  expired receiver snapshot. Physical panel blanking is not confirmed by software.
- Three renderer/ownership failures open the circuit. There is no timer-driven
  infinite restart. A subsequent fresh-data or device-absent/present edge can
  authorize one probe after a 60-second cooldown and two seconds of continuous
  fresh input. A failed probe blocks again. Thirty seconds of recent writes clear
  the failure budget. Old permanently blocked journals get one migration probe;
  new journals persist rearm and cooldown across service restarts. Missing data
  is not a USB failure, and does not consume this budget.
- The 12 ms inference tail is unchanged. In the separate Display Only process,
  snapshot/marker preparation precedes the same 12 ms wire budget, and navigation
  is not admitted with less than 4 ms remaining. Source snapshots still expire at
  0.3 seconds before send. Partial TCP writes invalidate the stream and reconnect
  through the existing 2/4/8-second display-only backoff. This does not prove that
  all observed send deadlines were caused by preparation: OS scheduling, USB
  backpressure and receiver behavior require device measurements.
- Diagnostics distinguish preparation time, send duration/budget, source age,
  STATE round-trip time, host receive interval/storage duration, capture duration
  and panel send duration. These are local durations, not a claimed one-way
  network latency or subtraction of unrelated boot clocks. Port 7000's detailed
  screen and diagnostic JSON/TXT expose the measurements and recovery reason.
- The existing FFmpeg mirror patch remains unchanged. Boot mirrors in landscape
  coordinates before portrait rotation using the same mirror bit; it retains
  only the last presentation setting in RAM. Vehicle mirror changes continue to
  use the existing live Params polling without restarting the encoder. Physical
  optical orientation still requires TURZX verification.

## Validation boundary

`test_hud_ncm_recovery.py` covers real Linux process flock handoff/return,
wrapped expiry classification, render delay blanking, recovery limits and old
blocked journals, DATA/RAM service override rollback, and isolated send budgets.
Existing tests cover actual localhost TCP reconnects (including 500 sessions),
disconnect/partial frames, H264/mirror behavior, settings/signals/parking/cameras,
and protected install/failed-install/rollback with simulated Linux services.
The new Actions workflow runs these and a 120-second localhost/FFmpeg benchmark,
then exports a protected package from the same tested commit.

No real Jetson, TURZX, comma, systemd reboot, USB-NCM cable disconnection, GPU,
TensorRT or optical display was exercised in this workstation environment.
`physical_hud_verified` remains false. Linux CI is not a substitute for those
device tests. DM, vehicle control, model bytes and vendor pin remain unchanged.

## Protected update and rollback

Use the package's matching `protected_install.py`, `protected_ncm.py`, manifest
and `SHA256SUMS`. Verify hashes before copying to existing DATA incoming storage.
The package retains L4T 36.4.7 / TensorRT 10.3.0, the original model SHA and the
fixed Carrot vendor revision. No APP or SETUP write is required.

With the vehicle parked/offroad, preserve the current DATA configuration and
release ID. Use the existing interface and model SHA from
`/opt/carrot-jetlink/updates/nexo-ncm.json`; do not invent a new interface/model.
For the existing Display Only installation:

```sh
sudo python3 protected_install.py install --bundle nexo-hud-recovery.tar.gz \
  --sha256 <bundle_sha256_from_manifest> --ncm-interface <existing_interface> \
  --model-sha256 <existing_model_sha256> --display-only --confirmed-offroad
```

Update comma to this branch too for sender changes and 7000 diagnostics. Retain
its previous clean branch/commit and preserve any local changes before switching.
Never use reset/clean to discard vehicle changes.

Check `NEXO_INSTALL_OK`, current/SOURCE_COMMIT, diagnostic timing/recovery fields,
the actual displayed speed, and live mirror changes. Test boot→HUD→boot, NCM
disconnect/reconnect, and cold reboot while parked. The boot override is replayed
from DATA by the existing protected activation hook at every boot.

Rollback remains `sudo python3 protected_install.py rollback --confirmed-offroad`.
The helper removes its RAM panel overrides before restoring an older release;
the original APP boot screen and previous model metadata remain intact. Return
comma to the preserved prior commit separately. Do not edit APP/SETUP or delete
the recovery journal as a routine recovery step.
