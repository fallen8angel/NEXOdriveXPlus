# Display Only HUD transient-gap recovery

Baseline: `dff111254462ba5152a8e4b8bc208c00b7fbbf50`. This is an optional display
path change. DM, controls, Panda, models, MED, parking decoding, normal HUD
layout/settings and the inference transport remain unchanged.

## Diagnosis

The reported 17 ms TCP RTT alone does not prove that a 12 ms socket write must
fail: send completion measures local kernel acceptance, not a remote ACK. The
code nevertheless closes the entire connection when a framed write cannot
complete inside `TAIL_BUDGET=.012`. Scheduling delays, large optional previews,
kernel backpressure or a temporarily slow reader can exhaust that budget with
no USB packet loss. A partially sent header must not be abandoned and followed
by another header on the same stream. The existing inference behavior remains
appropriate for its bounded tail and is untouched.

Previously the Jetson panel callback raised `SnapshotUnavailable` after a
500 ms input lease expired. The supervisor then admitted another renderer;
that necessarily reopened the encoder and USB display. This explains a
restart-driven blanking mechanism, but does not establish the initial cause of
the hardware's network delay. Broken pipe, connection refusal and host service
restarts require the paired device logs to distinguish cause from consequence.

## Changes

* `hud_tx.py` is enabled only after the separate NCM Display Only HELLO. It
  pumps nonblocking writes in 12 ms scheduling slices with RX polling between
  slices. A HUD message must complete before its producer timestamp + 300 ms.
  An unsent stale message can be discarded; a partially expired one closes the
  stream. Pending HUD messages coalesce to the latest generation. Queue size
  is bounded; STATE is inserted only at an idle boundary so receipts remain
  ordered, and optional navigation fragments yield under pressure.
* TCP retries retain the snapshot producer. Vehicle/model inference processes
  and their shared transport deadlines are not changed.
* `hud.py` replaces all vehicle/camera/navigation pixels with a neutral waiting
  frame before encoding if the capture's input has expired. A newer snapshot
  arriving mid-capture cannot refresh an older rendered frame. Explicit fresh
  offroad/disable still releases ownership to the existing boot display.
* `hud_encoder.py` adds AUD frame boundaries to the existing libx264 GOP=1
  session. It retains input freshness metadata, drops complete expired IDRs,
  and bounds pending input to four frames. It removes FFmpeg input `nobuffer`
  because that discards the probe frame and breaks metadata pairing. One frame
  of lookahead adds approximately 100 ms at 10 fps. GOP other than 1 is refused
  for this adapter; default settings already use 1. No arbitrary H264 chunk is
  dropped. A physical USB write already in progress is completed for framing;
  physical display latency is not measured or guaranteed by software checks.
* A running renderer must continue writing waiting frames during input loss.
  Encoder stalls/USB faults consume the bounded recovery budget (three failures,
  then existing limited probes). Input expiry alone does not restart anything.
* Clock bounds use host-send minus comma-receive during HELLO/STATE; receive age
  conservatively includes sender/transport delay without equating device boot
  clocks. Old peers retain their original receipt lease. Both devices must be
  updated for the new end-to-end clock bound.

## Evidence and deployment

7000's 8-second capture and the long developer log both already call
`health.diagnostic_evidence`; new fields include `connection_id`, HUD sequence,
separate comma/Jetson boot IDs and monotonic stamps, source age, queue depth,
write bytes/time/readiness, input gap history, display mode and expired encoded
frame count. `snapshot_producer_stalled`, `hud_receive_delayed`,
`tcp_interrupted`, `receiver_unavailable`, `encoder_output_stalled`, and USB
failures are separate observations. A stale/unavailable report is not proof of
a specific physical fault. Persistent errors retain the previous wire evidence.

Tests inject 60 ms send backpressure, partial deadlines, coalescing, broken pipe,
delayed STATE replies, source expiry with different boot clocks, lost input,
output stalls and both mirror modes. A real FFmpeg process encodes live,
waiting and recovered frames; decoding verifies stale-frame omission, frame
order and a single encoder PID/stream start. CI also checks protected rollback,
package imports, checksums and a 120-second local TCP/software-encoder soak.

Comma needs the normal source update; Jetson needs the separately generated
protected DATA ZIP. Its generated `INSTALL_KO.md` contains exact checksums,
Windows → comma ProxyJump transfer, preflight, installation and rollback
commands. APP/SETUP, mount layout and Display Only opt-in are unchanged.
Jetson hardware, actual panel pixels, scheduling latency, unplug/replug and
cold boot must still be checked on the parked device.
