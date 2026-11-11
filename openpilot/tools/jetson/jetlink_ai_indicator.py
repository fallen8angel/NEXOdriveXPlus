"""Read-only proof of live Jetson inference for the external HUD.

Do not confuse an enabled toggle, NCM connectivity, a ready TensorRT engine,
or a display-only session with completed external model inference.
The comma producer uses the *comma* monotonic clock to validate the status.
The Jetson renderer trusts only the bounded, fresh HUD snapshot.
"""
from __future__ import annotations

from pathlib import Path

from openpilot.tools.jetson.state import read_fresh

MODEL_STATUS = Path('/dev/shm/nexo-jetlink-model.json')
MODEL_NAME = 'NEXO native driving_supercombo'
MODEL_LEASE_S = 2.25


def verified_on_comma(now=None):
  """True only after modeld actually returned external inference results.

  JoiningModel writes active=True after its first successful client.infer().
  It reports ready=True only when the Jetlink model SHA matches its own model
  contract and the daemon is ready. Missing/stale/malformed data fail closed.
  """
  status = read_fresh(MODEL_STATUS, MODEL_LEASE_S, now=now, limit=4096)
  return bool(isinstance(status, dict)
              and status.get('active') is True
              and status.get('ready') is True
              and status.get('model') == MODEL_NAME)


def visible_from_snapshot(snapshot):
  """True ONLY for an explicit boolean from a separately freshness-checked HUD input."""
  return (isinstance(snapshot, dict)
          and snapshot.get('jetlink_inference_active') is True)


def install(renderer):
  """Add a Jetson-only, small green badge to every live external HUD layout.

  No service, model, or vehicle control is changed. On missing or expired
  snapshots the wrapper simply delegates to the existing renderer.
  """
  cls = renderer.ClusterUiRenderer
  if getattr(cls, '_nexo_jetlink_ai_badge_installed', False):
    return
  previous_draw = cls._draw_hud

  def draw(self, state, signal_lights=None):
    previous_draw(self, state, signal_lights)
    # The snapshot reader's 0.5 s lease ensures no stale 'AI ON' frame remains.
    from openpilot.tools.jetson.snapshot import read_snapshot
    if not visible_from_snapshot(read_snapshot()):
      return
    rl = renderer.rl
    rl.rl_push_matrix()
    rl.rl_scalef(self.width / renderer.DESIGN_WIDTH,
                  self.height / renderer.DESIGN_HEIGHT, 1.0)
    try:
      # Clear top-right corner; keep speed, gear, turn signals and camera open.
      x, y, width, height = renderer.DESIGN_WIDTH - 256.0, 29.0, 219.0, 49.0
      self._rounded_rect(x, y, width, height, 13.0, (12, 43, 35), (65, 221, 146), 2.0)
      self._rounded_rect(x + 13.0, y + 17.0, 14.0, 14.0, 7.0, (65, 221, 146))
      self._draw_text('JETSON AI ON', x + 37.0, y + height * .5, 23.0, (227, 255, 239))
    finally:
      rl.rl_pop_matrix()

  cls._draw_hud = draw
  cls._nexo_jetlink_ai_badge_installed = True
