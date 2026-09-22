"""Read-only external-HUD side-camera activation and crop geometry."""
from __future__ import annotations

import math
from typing import Literal


SideCameraSide = Literal["left", "right", "both"]


def side_camera_active_side(
    state,
    *,
    enabled: bool,
    trigger_mode: int,
    preview_mode: int = 0,
) -> SideCameraSide | None:
    """Resolve which driver-camera crop should be visible.

    Reverse always wins elsewhere in the renderer. Setup preview is deliberately
    restricted to Park at walking speed so a forgotten preview setting cannot
    cover the driving view.
    """
    if not bool(getattr(state, "onroad", False)) or bool(getattr(state, "reverse_active", False)):
        return None

    if preview_mode and str(getattr(state, "gear_text", "") or "").upper() == "P":
        try:
            speed_kph = abs(float(getattr(state, "speed_kph", 0.0)))
        except (TypeError, ValueError):
            speed_kph = math.inf
        if math.isfinite(speed_kph) and speed_kph <= 1.0:
            return {1: "left", 2: "right", 3: "both"}.get(int(preview_mode))

    if not enabled:
        return None

    left_signal = bool(getattr(state, "left_signal", False))
    right_signal = bool(getattr(state, "right_signal", False))
    # Do not treat hazard lights as a request for both blind-spot cameras.
    signal_left = left_signal and not right_signal
    signal_right = right_signal and not left_signal
    blind_left = bool(getattr(state, "left_blindspot", False))
    blind_right = bool(getattr(state, "right_blindspot", False))

    mode = max(0, min(2, int(trigger_mode)))
    left = (signal_left if mode in (0, 2) else False) or (blind_left if mode in (1, 2) else False)
    right = (signal_right if mode in (0, 2) else False) or (blind_right if mode in (1, 2) else False)
    if left and right:
        return "both"
    if left:
        return "left"
    if right:
        return "right"
    return None


SIDE_CAMERA_PANEL_WIDTH_RATIO = 0.39
SIDE_CAMERA_PANEL_HEIGHT_RATIO = 0.93
SIDE_CAMERA_PANEL_Y_RATIO = 0.035
SIDE_CAMERA_EDGE_MARGIN_RATIO = 0.018
SIDE_CAMERA_BOTH_X_RATIO = 0.188


def side_camera_panel_rect(
    screen_width: float,
    screen_height: float,
    side: SideCameraSide,
    width_ratio: float = SIDE_CAMERA_PANEL_WIDTH_RATIO,
    height_ratio: float = SIDE_CAMERA_PANEL_HEIGHT_RATIO,
) -> tuple[float, float, float, float]:
    """Place a single side-camera panel against the matching screen edge.

    Left requests hug the left edge, right requests hug the right edge. The
    existing two-pane preview keeps its previous position for compatibility.
    """
    sw = max(1.0, float(screen_width))
    sh = max(1.0, float(screen_height))
    # User-adjustable panel size. Clamp here as a second safety net even
    # though the renderer already constrains the persisted settings.
    width_ratio = max(0.20, min(0.50, float(width_ratio)))
    height_ratio = max(0.40, min(0.95, float(height_ratio)))
    panel_w = sw * width_ratio
    panel_h = sh * height_ratio
    panel_y = sh * SIDE_CAMERA_PANEL_Y_RATIO
    margin = sw * SIDE_CAMERA_EDGE_MARGIN_RATIO

    if side == "left":
        panel_x = margin
    elif side == "right":
        panel_x = sw - panel_w - margin
    else:
        panel_x = sw * SIDE_CAMERA_BOTH_X_RATIO

    panel_x = max(0.0, min(sw - panel_w, panel_x))
    return panel_x, panel_y, panel_w, panel_h


def source_crop_rect(
    frame_width: float,
    frame_height: float,
    destination_width: float,
    destination_height: float,
    center_x: float,
    center_y: float,
    zoom: float,
) -> tuple[float, float, float, float]:
    """Return a bounded source rect matching the destination aspect ratio."""
    fw = max(1.0, float(frame_width))
    fh = max(1.0, float(frame_height))
    dw = max(1.0, float(destination_width))
    dh = max(1.0, float(destination_height))
    cx = max(0.0, min(1.0, float(center_x)))
    cy = max(0.0, min(1.0, float(center_y)))
    z = max(1.0, min(4.0, float(zoom)))

    target_aspect = dw / dh
    frame_aspect = fw / fh
    if target_aspect >= frame_aspect:
        base_w = fw
        base_h = fw / target_aspect
    else:
        base_h = fh
        base_w = fh * target_aspect

    crop_w = max(1.0, min(fw, base_w / z))
    crop_h = max(1.0, min(fh, base_h / z))
    x = max(0.0, min(fw - crop_w, cx * fw - crop_w * 0.5))
    y = max(0.0, min(fh - crop_h, cy * fh - crop_h * 0.5))
    return (x, y, crop_w, crop_h)
