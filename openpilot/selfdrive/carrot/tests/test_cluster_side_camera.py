from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cluster"))

from cluster_side_camera import side_camera_active_side, side_camera_panel_rect, source_crop_rect


def state(**kwargs):
    values = dict(
        onroad=True,
        reverse_active=False,
        gear_text="D",
        speed_kph=30.0,
        left_signal=False,
        right_signal=False,
        left_blindspot=False,
        right_blindspot=False,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_turn_signal_and_bsm_triggers_are_independent():
    assert side_camera_active_side(state(left_signal=True), enabled=True, trigger_mode=0) == "left"
    assert side_camera_active_side(state(left_blindspot=True), enabled=True, trigger_mode=0) is None
    assert side_camera_active_side(state(right_blindspot=True), enabled=True, trigger_mode=1) == "right"
    assert side_camera_active_side(state(right_signal=True), enabled=True, trigger_mode=1) is None
    assert side_camera_active_side(state(left_signal=True), enabled=True, trigger_mode=2) == "left"
    assert side_camera_active_side(state(right_blindspot=True), enabled=True, trigger_mode=2) == "right"


def test_hazards_do_not_open_camera_but_bsm_can():
    hazards = state(left_signal=True, right_signal=True)
    assert side_camera_active_side(hazards, enabled=True, trigger_mode=0) is None
    assert side_camera_active_side(
        state(left_signal=True, right_signal=True, left_blindspot=True),
        enabled=True,
        trigger_mode=2,
    ) == "left"


def test_reverse_and_offroad_suppress_side_camera():
    assert side_camera_active_side(state(reverse_active=True, left_signal=True), enabled=True, trigger_mode=2) is None
    assert side_camera_active_side(state(onroad=False, left_signal=True), enabled=True, trigger_mode=2) is None


@pytest.mark.parametrize("preview,expected", [(1, "left"), (2, "right"), (3, "both")])
def test_setup_preview_only_works_parked_and_stopped(preview, expected):
    parked = state(gear_text="P", speed_kph=0.0)
    assert side_camera_active_side(parked, enabled=False, trigger_mode=2, preview_mode=preview) == expected
    assert side_camera_active_side(state(gear_text="D", speed_kph=0.0), enabled=False, trigger_mode=2, preview_mode=preview) is None
    assert side_camera_active_side(state(gear_text="P", speed_kph=2.0), enabled=False, trigger_mode=2, preview_mode=preview) is None




def test_panel_follows_turn_direction_and_hugs_matching_edge():
    width, height = 1920, 480
    left = side_camera_panel_rect(width, height, "left")
    right = side_camera_panel_rect(width, height, "right")
    both = side_camera_panel_rect(width, height, "both")

    left_x, left_y, panel_w, panel_h = left
    right_x, right_y, right_w, right_h = right

    assert left_x == pytest.approx(width * .018)
    assert right_x + right_w == pytest.approx(width * (1.0 - .018))
    assert left_x == pytest.approx(width - (right_x + right_w))
    assert left_y == pytest.approx(right_y)
    assert panel_w == pytest.approx(right_w)
    assert panel_h == pytest.approx(right_h)

    # Two-pane mode intentionally keeps the previous placement.
    assert both[0] == pytest.approx(width * .188)


def test_crop_rect_preserves_destination_aspect_and_stays_in_frame():
    x, y, w, h = source_crop_rect(1928, 1208, 750, 440, .25, .5, 1.8)
    assert 0 <= x <= 1928 - w
    assert 0 <= y <= 1208 - h
    assert w / h == pytest.approx(750 / 440)
    assert w < 1928 and h < 1208


def test_crop_rect_clamps_edge_centers():
    for cx, cy in ((0, 0), (1, 1), (-2, 4)):
        x, y, w, h = source_crop_rect(1928, 1208, 700, 430, cx, cy, 3)
        assert x >= 0 and y >= 0
        assert x + w <= 1928 + 1e-6
        assert y + h <= 1208 + 1e-6


def test_side_camera_overlays_normal_hud_instead_of_replacing_world(monkeypatch):
    import cluster_renderer
    from cluster_renderer import ClusterUiRenderer

    ui = ClusterUiRenderer.__new__(ClusterUiRenderer)
    ui._reverse_camera = Mock()
    ui._side_camera = Mock()
    ui._side_camera_side = Mock(return_value="left")
    ui._side_camera_settings = {}
    ui._turn_signal_lights = Mock(return_value=(True, False))
    ui._profile_start = Mock(return_value=0)
    ui._profile_add = Mock()
    ui._render_world = Mock()
    ui._draw_hud = Mock()
    ui._draw_alert_overlay = Mock()
    ui.screen_mode = -1

    draw_side = Mock()
    monkeypatch.setattr(cluster_renderer, "draw_side_camera_hud", draw_side)

    current = state(left_signal=True)
    ui.render(current)

    # The base world and standard HUD stay rendered; the side camera is an
    # additional overlay rather than a replacement screen.
    ui._render_world.assert_called_once_with(current, (True, False))
    ui._draw_hud.assert_called_once_with(current, (True, False))
    draw_side.assert_called_once_with(ui, current, ui._side_camera, "left", ui._side_camera_settings)
