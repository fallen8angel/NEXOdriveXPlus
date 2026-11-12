# NEXO Jetson safe maintenance regression (2026-11-12)

Evidence: 21:54 Jetson unified diagnostic and 21:55 comma 8-second diagnostic.

1. **AI readiness**: detect mismatched package/NCM model hashes, HUD-only runtime and missing TensorRT engine independently. This commit does **not** build an engine or activate AI. Actual build, synthetic inference and vehicle-control integration remain unverified; require a fully verified Offroad state and the existing protected installer before any future transition.
2. **HUD**: retain the existing neutral waiting frame and unchanged 500 ms source freshness gate. If the Korean font is absent, draw ASCII waiting text instead of crashing the renderer. Count completed waits shorter than 200 ms separately so a 105 ms observed wait can be distinguished from physical panel loss. Physical HUD pixels are not verified.
3. **LTE gateway**: report whether the current cellular uplink is the configured NAT interface and explicitly leave live Internet/DNS unverified. Never auto-enable; Offroad-only opt-in and existing route priority remain unchanged.
4. **Diagnostics**: do not infer `IsOffroad=true` solely from `IsOnroad=false`. Vehicle state must be independently verified. Existing camera JPEG-envelope evidence does not prove image decoding.
5. **Updates**: repair the malformed Git conflict regex so untracked files are preserved in a backup when a fast-forward pull conflicts. Reject absolute and parent-traversal entries. Tracked local modifications continue to block automatic updates rather than being reset.

No CAN/Panda control messages, steering/braking command paths, model engines, USB gadget ownership or live vehicle settings are changed. These are repository changes only; device installation and actual panel/AI/LTE verification are separate.
