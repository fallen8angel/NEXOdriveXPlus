"""Capture actual software-rendered NEXO P/D/R HUD frames with synthetic sample inputs.

This runs the installed production ClusterUiRenderer and Jetson gear hook, not a
hand-made mockup. It is NOT a physical USB HUD or live vehicle capture.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import time

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cluster"))
import cluster_renderer
from cluster_models import SimulatorInput, TpmsInfo, TripReportState
from cluster_simulator import ClusterSimulator
from openpilot.tools.jetson.gear_view import install


def packet(gear: str, tick: int):
  stamp = 100_000_000_000 + tick * 100_000_000
  return {
    "session": "sample", "sent": stamp / 1e9, "updated": time.monotonic(),
    "mono": {"carState": stamp},
    "valid": {"carState": True}, "alive": {"carState": True},
    "gear_view": {
      "gear": gear, "sample_ns": stamp,
      "can_ok": True, "park_enabled": True, "left": False, "right": False,
    },
  }


def main():
  original = cluster_renderer.ClusterUiRenderer
  class PreviewRenderer(original):
    pass
  cluster_renderer.ClusterUiRenderer = PreviewRenderer
  source = {}
  install(cluster_renderer, lambda: source, lambda _: "17:24:00")
  output = Path("build/hud-frames")
  output.mkdir(parents=True, exist_ok=True)

  ui = PreviewRenderer(width=1920, height=480)
  ui.open(hidden=True)
  state = replace(
    ClusterSimulator().update(SimulatorInput(), .1),
    onroad=True,
    fuel_gauge=0.72,
    energy_gauge_label="fuel",
    tpms=TpmsInfo(fl=32., fr=33., rl=31., rr=32.),
    trip_report=TripReportState(duration_s=600., distance_m=4920.,
                                average_speed_kph=41.8, auto_ratio_percent=92.),
  )
  tick = 0
  try:
    for gear, label in (("park", "P"), ("drive", "D"), ("reverse", "R")):
      for _ in range(4):
        tick += 1
        source.update(packet(gear, tick))
        rgba, w, h = ui.render_to_rgba_bytes(replace(state, gear_text=label))
      assert ui._jetson_gears.mode == gear, (label, ui._jetson_gears.mode)
      Image.frombytes("RGBA", (w, h), rgba).save(output / f"actual-software-render-{label}.png")
      print(f"Actual {label} software framebuffer: {w}x{h}, {output / f'actual-software-render-{label}.png'}")
  finally:
    ui.close()


if __name__ == "__main__":
  main()
