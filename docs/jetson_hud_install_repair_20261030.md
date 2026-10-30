# Protected HUD installation repair, 2026-10-30

Reported target: `8f04490116e8c9a3f2ddc206b309776ee691bf1c`.
Reported previous Jetson release: `4eabb4a77f14842c38586a36f27dad05d9b47f1c`.
No physical Jetson was accessed during this repair.

## Reproduced source and package defects

The NEXO source tree did not contain `openpilot/common/usbgpu_bus_lock.py`.
The protected builder exports NEXO's common directory, but only the pinned
Carrot `tools/jetlink` and `third_party/jetlink` directories. Its boot display
imports that common module inside `main()`. Merely importing the HUD did not
exercise it. Adding PYTHONPATH cannot supply a file absent from both exports.
The compatibility module is copied unchanged from ajouatom/carrot-jetson at
`d0dc96fa32410403dfb2fefff3e2909c11ec109b`; no vehicle code calls are changed.

The protected APP Xorg unit calls
`/opt/carrot-jetlink/current/tools/jetlink/xorg_auth.py jetlink` in ExecStartPre.
NEXO previously exported the helper only under `carrot-server/tools/jetlink`.
The new bundle provides the original entry point. A narrowly checked RAM
override supplies the vendor path when rolling back to an older NEXO release;
unrecognized pre-start commands are refused, not discarded. APP is unchanged.
This is a reproducible missing dependency. Without the device's full Xorg
journal and Condition definitions, it does not prove which dependency or
condition failed in the reported incident. ConditionResult=no alone is not
enough to identify the failed condition.

Display Only means no inference engine; it does not mean no graphics context.
The NEXO renderer calls Raylib/GLFW init_window and still requires Xorg.
Requires/After for protected storage and Xorg remain intact. The CPU JPEG
boot screen does not need Xorg. Its DATA override requires protected storage.
The existing boot/HUD panel lease, priority request, close-before-unlock order
and boot no-USB-reset policy are retained. Rollback to the older release without
the NEXO boot adapter restores APP's original boot display.

## Installation and recovery changes

- Exported files are checked for required entry points, boot/vendor dependencies
  and Python 3.10 syntax. The manifest includes the full static import inventory.
- Before stopping services, the actual Jetson venv imports the staged release
  in isolation as jetlink, in a disposable writable directory. This includes
  real adapters, schema, boot screen, TURZX lazy imports, serial, Crypto, PyUSB,
  video libraries, font loading, libusb backend and FFmpeg/libx264. It opens no
  USB device, renderer window, camera or model engine.
- Read-only systemd checks inspect loaded dependency units, Requires/After,
  candidate ExecStart/ExecStartPre paths and reevaluate all configured start
  conditions, preserving resets and OR groups. Historical ConditionResult is
  diagnostic evidence, not used as a substitute for reevaluation.
- install_hud_update.py --check-only now executes these checks instead of just
  printing the proposed command. It preserves Display Only, interface and model
  SHA from the existing protected configuration.
- Runtime/model/config restoration and service restoration are separate phases.
  A transaction is deleted and rolled_back recorded only after services stay
  active and, for NCM, the expected process owns the TCP listener. Failures keep
  the transaction and record rollback_failed. Boot recovery uses a separately
  ordered verification job so the updater never waits on services ordered after
  itself. The intermediate state is restored_pending_services.
- Missing renderer modules have a distinct renderer_import_failed category.
  Service diagnostics include LoadState, ConditionResult and AssertResult.
  A newly selected source commit can probe once after an old release's permanent
  block; restarting the same release preserves the retry budget. Existing input
  expiry, TCP, renderer, panel-write, timed retry and device-return behavior is
  preserved. No shared USB reset or extra renderer owner is introduced.

## Verification and limits

Windows/Python 3.12 development tests: 443 passed, 4 skipped in the Jetson suite;
510 passed, 6 skipped plus 22 subtests in additional HUD/Jetlink tests, with two
pre-existing failures reproduced in a separate unchanged 8f044901 checkout:

- test_spas_rear_positions_are_mapped_to_display_stages_and_expire
- test_connected_marker_clears_before_even_failed_encoder_teardown

The compiled Params-dependent cluster_autorun suite cannot collect in this PC
environment. Linux process/lock and full built vehicle-runtime checks are not
claimed as tested locally. The isolated extracted package imports actual HUD,
boot and vendor modules without stubs; parsing for Python 3.10 does not equal
execution on Jetson Python 3.10. The GitHub workflow additionally runs Linux
recovery tests, local TCP/FFmpeg soak and the extracted-package import check.

No changes to DM, Panda, steering, longitudinal control, MED, parking sensor
logic, HUD drawing logic, Display Only semantics, model bytes or wire format.
The remaining hardware acceptance is: actual target preflight, install and
rollback, Xorg/GL context, cold boot and interrupted boot recovery, boot-to-HUD
handoff, real USB/panel writes, cable reconnection and sustained display output.

## Package

Build only committed sources:

```sh
python -m openpilot.tools.jetson.build_hud_update --carrot /path/to/pinned/carrot-jetson NEXO-Jetson-HUD-repair.zip
```

The ZIP contains the committed tar.gz, manifest, matching installers,
Connect-Jetson.ps1, SHA256SUMS and INSTALL_KO.md with the exact source SHA,
bundle checksum and Windows/comma/Jetson commands. The outer ZIP checksum is
written beside the ZIP. It uses only the existing protected DATA layout.

Systemd semantics reference:
https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html
