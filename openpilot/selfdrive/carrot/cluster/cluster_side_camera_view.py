"""External-HUD blind-spot camera panel using the shared comma driver stream."""
from __future__ import annotations

import pyray as rl


def _camera_rects(renderer, side: str):
    w, h = renderer.width, renderer.height
    panel = rl.Rectangle(w * .188, h * .035, w * .392, h * .93)
    gap = max(6.0, w * .004)
    if side == "both":
        half = (panel.width - gap) * .5
        return panel, (
            ("left", rl.Rectangle(panel.x, panel.y, half, panel.height)),
            ("right", rl.Rectangle(panel.x + half + gap, panel.y, half, panel.height)),
        )
    return panel, ((side, panel),)


def draw_side_camera_hud(renderer, state, camera_session, side: str, settings: dict[str, object]) -> None:
    panel, panes = _camera_rects(renderer, side)
    rl.draw_rectangle_rounded(
        rl.Rectangle(panel.x - 3, panel.y - 3, panel.width + 6, panel.height + 6),
        .08, 18, rl.Color(93, 108, 120, 255),
    )
    rl.draw_rectangle_rounded(panel, .08, 18, rl.Color(3, 8, 12, 255))

    for pane_side, pane in panes:
        blindspot = bool(
            getattr(state, "left_blindspot" if pane_side == "left" else "right_blindspot", False)
        )
        accent = rl.Color(255, 64, 58, 255) if blindspot else rl.Color(255, 174, 54, 255)
        inner = rl.Rectangle(pane.x + 5, pane.y + 5, pane.width - 10, pane.height - 10)
        rl.draw_rectangle_rec(inner, rl.BLACK)
        rl.begin_scissor_mode(int(inner.x), int(inner.y), int(inner.width), int(inner.height))
        try:
            center_x = float(settings["left_x"] if pane_side == "left" else settings["right_x"])
            crop = (center_x, float(settings["center_y"]), float(settings["zoom"]))
            if not camera_session.draw(inner, crop=crop):
                rl.draw_rectangle_rec(inner, rl.BLACK)
                renderer._draw_text(
                    "카메라 연결 대기",
                    inner.x + inner.width * .5,
                    inner.y + inner.height * .5,
                    renderer.height * .046,
                    (177, 187, 199),
                    anchor="center",
                )
        finally:
            rl.end_scissor_mode()

        rl.draw_rectangle_lines_ex(
            rl.Rectangle(pane.x + 2, pane.y + 2, pane.width - 4, pane.height - 4),
            4.0,
            accent,
        )
        label = "좌측 사각지대" if pane_side == "left" else "우측 사각지대"
        renderer._draw_text(
            label,
            pane.x + pane.width * .5,
            pane.y + pane.height - renderer.height * .065,
            renderer.height * .042,
            (255, 226, 182) if not blindspot else (255, 206, 203),
            anchor="center",
        )
