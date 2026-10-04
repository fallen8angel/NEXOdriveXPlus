from __future__ import annotations

import json
from pathlib import Path
import socket
import time
from typing import Any

import pyray as rl


JETSON_HUD_STATUS_PORT = 8767
JETSON_STATUS_MAGIC = "NEXO_JETSON_STATUS"
JETSON_STATUS_STALE_SECONDS = 2.5
JETSON_SOCKET_RETRY_SECONDS = 5.0

NVIDIA_STATUS_CENTER_X = 470.0
NVIDIA_STATUS_CENTER_Y = 55.0
NVIDIA_STATUS_SIZE = 42.0
NVIDIA_ICON_PATH = Path(__file__).resolve().parents[2] / "assets" / "icons_mici" / "nvidia.png"
VNAVI_STATUS_CENTER_X = 470.0
VNAVI_STATUS_CENTER_Y = 99.0
STATUS_BADGE_HEIGHT = 34.0
VNAVI_BADGE_WIDTH = 104.0
STATUS_BADGE_RADIUS = 7.0
STATUS_FONT_SIZE = 20.0
STATUS_BG = (0, 0, 0, 168)
STATUS_STROKE = (0, 0, 0, 255)
VNAVI_CYAN = (72, 220, 255, 255)


def _open_jetson_socket(renderer: Any, now: float):
    sock = getattr(renderer, "_jetson_hud_status_socket", None)
    if sock is not None:
        return sock

    retry_at = float(getattr(renderer, "_jetson_hud_socket_retry_at", 0.0) or 0.0)
    if now < retry_at:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", JETSON_HUD_STATUS_PORT))
        sock.setblocking(False)
        renderer._jetson_hud_status_socket = sock
        renderer._jetson_hud_last_seen_t = 0.0
        renderer._jetson_hud_connected = False
        return sock
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        renderer._jetson_hud_status_socket = None
        renderer._jetson_hud_socket_retry_at = now + JETSON_SOCKET_RETRY_SECONDS
        return None


def _jetson_connected(renderer: Any) -> bool:
    now = time.monotonic()
    sock = _open_jetson_socket(renderer, now)
    if sock is None:
        return False

    for _ in range(16):
        try:
            data, _addr = sock.recvfrom(65535)
        except BlockingIOError:
            break
        except OSError:
            try:
                sock.close()
            except Exception:
                pass
            renderer._jetson_hud_status_socket = None
            renderer._jetson_hud_socket_retry_at = now + JETSON_SOCKET_RETRY_SECONDS
            return False

        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if payload.get("magic") != JETSON_STATUS_MAGIC:
            continue

        renderer._jetson_hud_last_seen_t = now
        # The mark means Jetson is actually connected to comma, not merely powered
        # and visible on the same network.
        renderer._jetson_hud_connected = bool(payload.get("comma_tcp", False))

    last_seen = float(getattr(renderer, "_jetson_hud_last_seen_t", 0.0) or 0.0)
    if last_seen <= 0.0 or now - last_seen > JETSON_STATUS_STALE_SECONDS:
        renderer._jetson_hud_connected = False
    return bool(getattr(renderer, "_jetson_hud_connected", False))


def _vnavi_active(state: Any) -> bool:
    label = str(getattr(state, "cruise_override_label", "") or "").strip().lower()
    target = getattr(state, "cruise_override_kph", None)
    return label == "vnavi" and target is not None


def _draw_badge(renderer: Any, text: str, center_x: float, center_y: float, width: float, color) -> None:
    renderer._rounded_rect(
        center_x - width * 0.5,
        center_y - STATUS_BADGE_HEIGHT * 0.5,
        width,
        STATUS_BADGE_HEIGHT,
        STATUS_BADGE_RADIUS,
        STATUS_BG,
        color,
        1.4,
    )
    renderer._draw_text_with_stroke(
        text,
        center_x,
        center_y,
        STATUS_FONT_SIZE,
        color,
        STATUS_STROKE,
        1,
        anchor="center",
    )


def _nvidia_texture(renderer: Any):
    texture = getattr(renderer, "_jetson_nvidia_texture", None)
    if texture is not None:
        return texture
    if not NVIDIA_ICON_PATH.is_file():
        return None
    try:
        texture = rl.load_texture(str(NVIDIA_ICON_PATH))
    except Exception:
        return None
    renderer._jetson_nvidia_texture = texture
    return texture


def _draw_nvidia_logo(renderer: Any) -> None:
    texture = _nvidia_texture(renderer)
    if texture is None:
        return
    source = rl.Rectangle(0.0, 0.0, float(texture.width), float(texture.height))
    dest = rl.Rectangle(
        NVIDIA_STATUS_CENTER_X - NVIDIA_STATUS_SIZE * 0.5,
        NVIDIA_STATUS_CENTER_Y - NVIDIA_STATUS_SIZE * 0.5,
        NVIDIA_STATUS_SIZE,
        NVIDIA_STATUS_SIZE,
    )
    rl.draw_texture_pro(texture, source, dest, rl.Vector2(0.0, 0.0), 0.0, rl.WHITE)


def _draw_status_overlay(renderer: Any, state: Any) -> None:
    if _jetson_connected(renderer):
        _draw_nvidia_logo(renderer)

    if _vnavi_active(state):
        _draw_badge(
            renderer,
            "vNAVI",
            VNAVI_STATUS_CENTER_X,
            VNAVI_STATUS_CENTER_Y,
            VNAVI_BADGE_WIDTH,
            VNAVI_CYAN,
        )


def install_renderer_status_overlay(renderer_module: Any) -> None:
    renderer_cls = renderer_module.ClusterUiRenderer
    if bool(getattr(renderer_cls, "_nexo_status_overlay_installed", False)):
        return

    original_render = renderer_cls.render
    original_close = renderer_cls.close

    def render_with_status(self, state, signal_lights=None):
        original_render(self, state, signal_lights)
        try:
            _draw_status_overlay(self, state)
        except Exception:
            # A cosmetic status indicator must never break the external HUD.
            pass

    def close_with_status(self):
        sock = getattr(self, "_jetson_hud_status_socket", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
            self._jetson_hud_status_socket = None

        texture = getattr(self, "_jetson_nvidia_texture", None)
        if texture is not None:
            try:
                rl.unload_texture(texture)
            except Exception:
                pass
            self._jetson_nvidia_texture = None

        return original_close(self)

    renderer_cls.render = render_with_status
    renderer_cls.close = close_with_status
    renderer_cls._nexo_status_overlay_installed = True
