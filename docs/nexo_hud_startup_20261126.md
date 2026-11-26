# NEXO external HUD ignition animation

Scope: 2019 Hyundai NEXO, Comma4 sender, protected Jetson USB HUD. Original user
asset `nexo_startup.gif`: SHA256
`5d6ced6f52d0c642ba035b4e58a17a63d868eb6cd51bc817f0bcf7fc086a2b71`, 553x309,
nine frames at 230 ms each (2.07 seconds). No vehicle/logo redesign. Landscape
aspect-fit black padding, then the existing mirror/portrait transform. The final
frame crossfades for 300 ms to the already rendered, unchanged P scene. Production
output remains 10 FPS, so GIF presentation is quantized to the existing HUD ticks.

## Ignition identity and interruption

`hud_ignition.py` is an isolated, receive-only, nice(10) Comma process. It observes
fresh valid known `pandaStates.ignitionLine/ignitionCan` independently of onroad,
HUD and USB connection state. A continuous confirmed OFF lasting at least one
second followed by ON creates a durable UUID in `/data/nexo-hud/ignition.json`.
Unknown/empty/stale panda data never becomes OFF. A process restart reuses history.
Only a fresh heartbeat is forwarded as optional `hud_startup` snapshot metadata.

The Jetson commits a claim in `/opt/carrot-jetlink/updates/hud-startup.sqlite3`
before its first animation pixel. Service/process/USB restarts and DATA release
switches do not erase that database. Failed claims skip decoration. A crash may
therefore skip a partially played animation, but does not replay it. D/R/N or
motion observed by the independent Comma process inhibits the entire opportunity.
Admission requires fresh valid stationary P and selfdriveState, no warning,
and enabled park scene. Only the exact informational P-gear notice is exempt.
Animations are rejected 90 seconds after the observed ON edge. Every rendered
frame checks both its consumed snapshot and the latest snapshot. New gear,
warnings, blinkers or stale input cancel playback and never resume it that cycle.
Priority means the next available normal HUD frame, not zero physical latency.

**Known limitation:** without an observed OFF, software cannot distinguish a
new ignition cycle from a computer/link restart. On first install, corrupt/lost
history, or power removal before OFF was observed, this implementation deliberately
skips animation until a confirmed OFF->ON. It cannot promise playback on every
cold start. A physical ignition latch or vehicle-backed persistent cycle counter
would be needed to guarantee both every cold start and no restart duplicates
when the OFF transition is unobservable. This requires vehicle evidence, not a
guessed CAN mapping. Comma and Jetson must both be updated; an old sender simply
continues its normal HUD without animation.

## Existing display and model isolation

Reviewed `boot_display.py`, `display_owner.py`, `gear_view.py`, `hud.py`,
`nexo_park.jpg`, native/NCM snapshot transport and protected DATA updater.
Animation runs only inside the existing guarded RGBA capture after normal scene
rendering. It does not open USB, change ownership, reset the bus, reconfigure the
encoder, change brightness or sleep in the render loop. The boot diagnostic
adapter, lock handoff, park/reverse drawing and original JPEG are unchanged.
One background CPU decoder is bounded to 2 MiB encoded, 16 MiB decoded, 60 frames
and six seconds. For this asset decoded RGBA is 6,151,572 bytes; one 1920x462
compositor cache is 3,548,160 bytes, plus temporary PIL/output allocations.
Missing/corrupt assets, claim storage and decode failures fall through to normal HUD.
The existing 500 ms input lease and encoder expiry checks remain in force.

No driving-model, N16 registry, TensorRT, modeld inference, controls, CAN/DBC,
DM or vehicle safety code changed. The process registry adds only the optional
display observer. Model SHA256 remains
`f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5`.

## Public-source comparison (2026-11-26 KST)

NEXO baseline: `713b91d344f151cc63fb555d86d39e7f69ea4012`.

- [ajouatom/openpilot](https://github.com/ajouatom/openpilot/tree/47b1d349ca0bb26cfe99b3908d83108c0d200ad3),
  carrot-wip head: `47b1d349ca0bb26cfe99b3908d83108c0d200ad3`.
- [countpia-a/openpilot](https://github.com/countpia-a/openpilot/tree/ad0943c0fdfdd7a08762f73f5b88f30090e129b8),
  carrot-wip head: `ad0943c0fdfdd7a08762f73f5b88f30090e129b8`.
- [ajouatom/carrot-jetson](https://github.com/ajouatom/carrot-jetson/tree/d0dc96fa32410403dfb2fefff3e2909c11ec109b),
  main: `d0dc96fa32410403dfb2fefff3e2909c11ec109b`, unchanged pinned runtime.

CarrotPilot `tools/jetlink/boot_display.py` uses CPU JPEG diagnostics, a shared
panel lease, no-reset protection and retains the last picture during handoff.
Its current firmware status additions describe Carrot's Super firmware staging;
they are not applicable to this NEXO DATA animation and are not imported.
`tools/jetlink/hud.py` waits for supported USB, rechecks presence before opening,
caches read-only settings, and uses 10 FPS/libx264 with one FFmpeg thread. NEXO
already has these principles plus its own bounded supervision and freshness guard;
the new animation deliberately stays inside that existing session.

CarrotAssist is the Android companion under `tools/carrot_android`, not another
Jetson USB renderer. Its `VoiceWelcome.java` asynchronously prepares greeting
resources, uses generation IDs to reject stale callbacks, supports cancellation
and has a bounded fallback timeout. `ConnectionRecovery.java` cancels workers
and bounds WebView/network recovery. The applicable design ideas here are a
background asset loader, cancellation, finite duration and fallback; no Android,
voice, settings, driving-command or WebView feature was imported. Its app-open
greeting identity is unsuitable for physical ignition-cycle tracking.

## Verification and deployment boundaries

`tests/test_startup_animation.py` covers persistent OFF/ON history, corrupt history,
source write failure, stale/unknown panda input, persistent claims, repeated HUD/USB
recovery, new cycles, safety/gear interruption, timing, fade, mirror/portrait,
corrupt GIFs, late joins and input-expiry cancellation. Existing HUD and framebuffer
regressions must also pass. Linux CI runs protected install/rollback and deployment
import checks; `protected_probe` decodes the packaged GIF before service replacement.
The protected updater retains its existing preflight, transaction and rollback.

`python -m openpilot.tools.jetson.preview_startup OUTPUT` creates production-renderer
offline previews, comparison PNGs, GIF/MP4 and workstation metrics (Pillow, raylib,
imageio-ffmpeg, psutil required on the preview machine). They are synthetic stationary
P inputs and are **not vehicle recordings or measured Jetson GPU performance**.
Deployment uses the existing `build_hud_update` ZIP and included `INSTALL_KO.md`.
Never erase ignition/claim state when installing or rolling back.

Keep three statuses separate: committed source; installed on Jetson; physically
verified on NEXO. Real ignition timing/power-off capture, physical USB removal,
panel pixels/flicker, rear-view priority latency, CPU/GPU/RAM/temperature under N16
load and real interrupted-update rollback remain hardware acceptance work.
