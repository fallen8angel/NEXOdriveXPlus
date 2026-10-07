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
