# NEXO optional USB display link

Vehicle baseline: `757102f3f0a18ec1533c4c46fa712ffbf96d7645` (`origin/NEXO`).
Working branch: `jetson-usb-hud-20261007`. No NEXO branch push is implied.

Reference: ajouatom/openpilot `carrot-wip`, pinned at
`eb5bbf2720286f17fc55d6eab8a2f7aacecf1f3d`.

The MIT Jetlink stream framing, FunctionFS descriptor/endpoint lifecycle,
partial libusb I/O, write watchdog and background scheduling in
`third_party/jetlink/jetlink/transport` were reviewed and selectively adapted
into `transport/`. Their copyright remains in each source file and
`LICENSE.jetlink`. `setup_gadget.sh` derives from the reference's script.

NEXO changes: separate display protocol magic and strict message allowlist,
1 MiB frame cap, 2 MiB gadget receive queue, no borrowed model endpoints or
inference handoff, no realtime reader scheduling, separate gadget name and USB
product identity. The test VID/PID is shared with the reference, so the host
also verifies the product string before claiming the interface. This is an
experimental device identity, not an allocated production USB product ID.

No Cinque, TensorRT inference service, DM, vehicle control, remote shutdown,
remote code upload, AGNOS update, CAN publisher or remote Params writer is
included. Jetson deployment is explicit; there is no automatic host updater.

The Comma is the USB device (FunctionFS); Jetson is the USB host. The setup
script refuses to steal an occupied UDC and does not alter the kernel or AGNOS.
An unsupported kernel or USB role leaves this optional link disconnected.
The existing vehicle model and controls continue independently.

## Selective review on 2026-10-11

Reviewed `ajouatom/openpilot` carrot-wip through
`c6925561` and `ajouatom/carrot-jetson` main at the already-pinned `d0dc96f`.
Adapted the early HUD connection-marker cleanup from `59f54b05` and
reader-startup error reporting from `3f2fe40a`. NEXO keeps its own renderer,
normal-priority reader scheduling and exact native-model contract. The native
Jetson home/road badge follows Carrot's fresh peer-evidence approach, using only
NEXO status files. No App-selected models or mobile transport were imported.

The C4 brightness-independent road-view mode (`e74e6938`) and H264 poll deadline
fix (`59f54b05`) were already present. Experimental signal-stop assistance,
other-vehicle control changes, model selection and YouTube removal were not
imported. Native inference still has no HUD snapshot publisher: a HUD directly
attached to Comma is supported separately, while Jetson display-only mode
continues to yield its USB controller to native inference.

## Signal follow-up on 2026-10-11

At the user's subsequent request, signal observation and optional stop assistance
from `c6925561` were separately adapted to NEXO. See
[`SIGNAL_UPSTREAM.md`](../../selfdrive/carrot/SIGNAL_UPSTREAM.md). All per-device
signal flags remain OFF by default. The native Jetlink model contract, transport
and HUD paths are unchanged by that integration.
