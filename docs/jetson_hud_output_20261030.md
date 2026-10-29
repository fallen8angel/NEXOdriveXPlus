# HUD output interruption and missing diagnostics (2026-10-30)

Reported evidence: TCP connected, HELLO true, HUD TX OK, panel report NO,
renderer PID/last write null, recovery empty. No Jetson journal was available.
These observations locate the missing output evidence; they do not establish
whether the installed service failed, an old host omitted reports, or USB open
failed. Physical root cause remains unconfirmed.

## Reproducible code defects and changes

- Display Only previously stopped HUD sends while synchronously waiting up to
  500 ms for STATE, equal to the host snapshot lease. STATE is now polled with
  a single outstanding request and its original 500 ms response deadline; HUD
  sends continue. Each receipt is checked against the HUD sequence at request
  time. Partial replies remain buffered. Inference transport/client and its
  12 ms total tail are unchanged.
- PID reporting was gated on a recent panel write, hiding processes blocked in
  USB open. PID/running status now comes from the live supervisor; panel write
  success remains a separate, expiring observation, never physical pixel proof.
- Missing recovery files were silently omitted. New reports distinguish fresh,
  stale, missing and older hosts that do not report the field. Missing data uses
  unknown/null values, never a fabricated successful recovery or false block.
- Renderer import, ownership, USB open, first-frame and streaming stages are
  recorded. Errors include category, exception type and bounded detail, plus
  the Jetson wall-clock error timestamp. The supervisor records forced exits
  and preserves last successful write time. Raw journals are not relayed.
- Startup gets bounded per-stage deadlines with a 60 s absolute cap, rather
  than being killed after the steady-state 10 s watchdog during lock handoff
  and initialization. The 10 s watchdog remains for ongoing output stalls.
- After three panel failures, transient panel errors allow at most two timed
  probes (60 s then 300 s). Persistent failure stays blocked; observed data or
  device return can rearm recovery. This budget survives supervisor restarts;
  30 s healthy writes resets it. Healthy renderers are not restarted.
- A bounded background observer reads only systemd properties of
  carrot-jetlink-hud. It cannot block the HUD TCP receive loop. Unavailable
  systemd observations are explicit. The 7000 report and long development logs
  use the same evidence, installed revisions, failure stages and last errors.
- RAM-only protected service overrides select the current DATA HUD entrypoint
  and writable runtime working directory. The protected installer accepts an
  already stopped HUD only for existing Display Only repairs with an active
  server; storage/model/entrypoint/rollback checks remain mandatory.

## Installation and hardware acceptance

Update comma to the tested NEXO commit using its normal updater while offroad.
The Jetson also needs the matching protected package: updating comma alone does
not replace its renderer or add host service reports.

Copy the matching package, manifest, SHA256SUMS, protected_install.py,
protected_ncm.py and install_hud_update.py to existing DATA incoming storage.
Verify `sha256sum -c SHA256SUMS`. Preserve current/SOURCE_COMMIT and the installed
NCM configuration. The wrapper reads the existing interface/model identity:

```sh
python3 install_hud_update.py --bundle nexo-hud-recovery.tar.gz --sha256 <verified_bundle_sha256> --check-only
sudo python3 install_hud_update.py --bundle nexo-hud-recovery.tar.gz --sha256 <verified_bundle_sha256> --confirmed-offroad
```

Rollback: `sudo python3 protected_install.py rollback --confirmed-offroad`.
Restore comma's previous revision separately. No APP/SETUP modification or USB
gadget reset is part of these installation steps.

Read-only checks on the Jetson (requires actual device access):

```sh
cat /opt/carrot-jetlink/current/SOURCE_COMMIT
systemctl show carrot-jetlink-hud.service -p ActiveState -p SubState -p Result -p MainPID -p ExecMainStatus -p NRestarts
systemctl cat carrot-jetlink-hud.service carrot-jetlink-boot-display.service
journalctl -u carrot-jetlink-hud.service -b -n 150 --no-pager
cat /dev/shm/nexo-jetson/hud-recovery.json
cat /dev/shm/nexo-jetson/hud-stage.json
cat /dev/shm/nexo-jetson/hud-service.json
lsusb
```

Still required offroad: actual TURZX USB open/output, boot/HUD handoff, cable
disconnect/reconnect, cold boot, live mirror, vehicle display modes, and Jetson
install/rollback. CI/localhost/mocked systemd checks do not verify these.
DM, Panda, steering, longitudinal control, MED and parking/display rendering
logic and model bytes are unchanged.
