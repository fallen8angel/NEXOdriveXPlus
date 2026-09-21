from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cluster'))

from cluster_driver_feed import DriverCameraFeed
from cluster_live_camera import LiveDriverCamera, LiveRoadCamera
from cluster_parking import NexoParkingTracker
from cluster_reverse import RearParkingState, RearSensor, ReverseCameraSession, contained_rect, with_reverse_hud_state
from cluster_reverse_view import front_sensor_sectors, rear_sensor_sectors
from cluster_scene import build_cluster_scene
from test_cluster_blindspot_road import state
from test_cluster_parking import frame


@pytest.mark.parametrize('gear', ['D', 'P', 'N', None, 'R'])
@pytest.mark.parametrize('onroad,valid', [(True, True), (False, True), (True, False)])
def test_reverse_activation_and_exit(gear, onroad, valid):
    result = with_reverse_hud_state(state(gear_text=gear, onroad=onroad), valid, RearParkingState())
    assert result.reverse_active == (gear == 'R' and onroad and valid)


def test_spas_rear_positions_are_mapped_to_display_stages_and_expire():
    tracker = NexoParkingTracker()
    tracker.observe([frame("0001401118100C71")], 10, 10)
    data = tracker.current_rear(10)
    raw = dict(data.raw_codes)
    assert raw["ROL"] == 0
    assert raw["RIL"] == 1
    assert raw["RI"] == 2
    assert raw["RIR"] == 2
    assert raw["ROR"] == 3
    assert [sensor.position for sensor in data.sensors] == ["ROL", "RIL", "RI", "RIR", "ROR"]
    assert [sensor.proximity for sensor in data.sensors] == [None, "far", "near", "near", "very_near"]
    assert tracker.current_rear(11.01) == RearParkingState()
    tracker.observe([], 11, 11, valid=False)
    assert tracker.current_rear(11) == RearParkingState()


@pytest.mark.parametrize('lateral', [-.8, -.27, .27, .8])
def test_only_selected_physical_sensor_is_drawn(lateral):
    sensors = tuple(RearSensor(str(x), x, x == lateral) for x in (-.8, -.27, .27, .8))
    sectors = rear_sensor_sectors(sensors, 1920, 480)
    assert len(sectors) == 1
    for cx, cy, inner, outer, start, end, color in sectors:
        assert (start + end) / 2 == pytest.approx(90 + lateral * 58)
        assert end > start and outer > inner > 0
        assert cx == pytest.approx(1920 * .445)
        assert cy + outer < 480


def test_front_and_rear_sensor_geometry_are_separate():
    front = (RearSensor("FI", 0, True, "near", "front", 2),)
    rear = (RearSensor("RI", 0, True, "near", "rear", 2),)
    front_sectors = front_sensor_sectors(front, 1920, 480)
    rear_sectors = rear_sensor_sectors(rear, 1920, 480)
    assert len(front_sectors) == 2
    assert len(rear_sectors) == 2
    assert all((start + end) / 2 == pytest.approx(270) for *_, start, end, _ in front_sectors)
    assert all((start + end) / 2 == pytest.approx(90) for *_, start, end, _ in rear_sectors)


def test_no_detection_unknown_and_validated_levels():
    for detected in (None, False):
        assert not rear_sensor_sectors((RearSensor('a', 0, detected),), 1920, 480)
    colors = []
    for level, count in [('far', 1), ('near', 2), ('very_near', 3)]:
        sectors = rear_sensor_sectors((RearSensor('a', 0, True, level),), 1920, 480)
        assert len(sectors) == count
        colors.append(sectors[0][-1])
    assert len(set(colors)) == 3


def test_camera_failure_retry_and_repeated_release():
    now = [0.0]
    cameras = []
    def factory():
        camera = Mock()
        camera.draw.return_value = True
        cameras.append(camera)
        return camera
    session = ReverseCameraSession(factory, lambda: now[0])
    for _ in range(200):
        assert session.draw(None)
        camera = session.camera
        assert session.draw(None)
        assert session.camera is camera
        session.close()
        session.close()
        camera.close.assert_called_once()
        assert session.camera is None
    assert len(cameras) == 200
    session.draw(None)
    cameras[-1].draw.side_effect = RuntimeError('lost')
    assert not session.draw(None)
    assert session.camera is None
    assert not session.draw(None)
    assert len(cameras) == 201
    now[0] = 1.1
    assert session.draw(None)
    session.close()


@pytest.mark.parametrize('size', [(1928, 1208), (640, 480), (480, 640)])
def test_camera_fits_without_crop(size):
    x, y, w, h = contained_rect(*size, (100, 50, 650, 360))
    assert x >= 100 and y >= 50 and x + w <= 750 and y + h <= 410
    assert w / h == pytest.approx(size[0] / size[1])


def test_driver_view_reuses_draw_with_mirroring_and_letterbox(monkeypatch):
    draw = Mock(return_value=True)
    monkeypatch.setattr(LiveRoadCamera, 'draw', draw)
    camera = LiveDriverCamera.__new__(LiveDriverCamera)
    destination = object()
    assert camera.draw(destination)
    draw.assert_called_once_with(destination, fit=True, mirror=True)


def test_blocked_connect_never_blocks_hud_or_spawns_per_reverse():
    entered, release = threading.Event(), threading.Event()
    def connect(_):
        entered.set()
        release.wait(2)
        return False
    client = Mock()
    client.is_connected.return_value = False
    client.connect.side_effect = connect
    factory = Mock(return_value=client)
    feed = DriverCameraFeed(factory)
    try:
        feed.set_active(True)
        assert entered.wait(1)
        worker = feed._thread
        for _ in range(200):
            feed.set_active(False)
            assert feed.snapshot() is None
            feed.set_active(True)
            assert feed._thread is worker
        assert factory.call_count == 1
        feed.close()
        assert feed.snapshot() is None
    finally:
        release.set()
        feed.close()
        feed._thread.join(2)
    assert not feed._thread.is_alive()


def test_gpu_owner_released_on_loss_and_reconnect():
    camera = LiveDriverCamera.__new__(LiveDriverCamera)
    owner, frame1 = object(), object()
    camera._frame_owner = None
    camera._last_frame_at = 0
    camera._destroy_egl_images = Mock()
    camera._clear_copy_textures = Mock()
    camera._feed = Mock()
    camera._feed.snapshot.return_value = (owner, frame1, 1)
    camera._poll_frame(1)
    assert camera._frame is frame1 and camera._frame_owner is owner
    camera._feed.snapshot.return_value = None
    camera._poll_frame(3)
    assert camera._frame is None and camera._frame_owner is None
    assert camera._destroy_egl_images.call_count == 2


def test_feed_delivers_latest_recovers_and_clears_on_exit():
    clients = []
    available = threading.Event()
    def factory():
        client = Mock()
        client.is_connected.return_value = True
        client.num_buffers = 4
        client.recv.side_effect = lambda **_: object() if available.is_set() else None
        clients.append(client)
        return client
    feed = DriverCameraFeed(factory)
    try:
        feed.set_active(True)
        available.set()
        deadline = time.monotonic() + 2
        while feed.snapshot() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert feed.snapshot() is not None
        first = feed.snapshot()[0]
        available.clear()
        deadline = time.monotonic() + 2
        while feed.snapshot() is not None and time.monotonic() < deadline:
            time.sleep(.01)
        assert feed.snapshot() is None
        available.set()
        deadline = time.monotonic() + 2
        while feed.snapshot() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert feed.snapshot() is not None
        feed.set_active(False)
        assert feed.snapshot() is None
        feed.set_active(True)
        deadline = time.monotonic() + 2
        while feed.snapshot() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert feed.snapshot() is not None
        assert feed.snapshot()[0] is not first
    finally:
        feed.close()
        feed._thread.join(2)
    assert not feed._thread.is_alive()


def test_driver_draw_uses_entire_frame_and_centers_in_right_panel():
    import pyray as rl
    camera = LiveRoadCamera.__new__(LiveRoadCamera)
    camera._ensure_connection = Mock(return_value=True)
    camera._poll_frame = Mock()
    camera._frame = SimpleNamespace(width=1928, height=1208)
    camera._zero_copy = False
    camera._draw_copy = Mock(return_value=True)
    destination = rl.Rectangle(1200, 60, 650, 360)
    assert camera.draw(destination, fit=True, mirror=True)
    source, actual = camera._draw_copy.call_args.args
    assert (source.x, source.y, source.width, source.height) == (0, 0, -1928, 1208)
    assert actual.x >= destination.x and actual.y >= destination.y
    assert actual.x + actual.width <= destination.x + destination.width + .001
    assert actual.width / actual.height == pytest.approx(1928 / 1208)


def test_reverse_dispatch_bypasses_normal_hud_and_restores_on_exit(monkeypatch):
    import cluster_renderer
    from cluster_renderer import ClusterUiRenderer
    ui = ClusterUiRenderer.__new__(ClusterUiRenderer)
    ui._reverse_camera = Mock()
    ui._close_live_road_camera = Mock()
    ui._draw_alert_overlay = Mock()
    ui._turn_signal_lights = Mock(return_value=(False, False))
    ui._profile_start = Mock(return_value=0)
    ui._profile_add = Mock()
    ui._render_world = Mock()
    ui._draw_hud = Mock()
    ui.screen_mode = -1
    draw = Mock()
    monkeypatch.setattr(cluster_renderer, 'draw_reverse_hud', draw)
    for _ in range(20):
        ui.render(state(reverse_active=True))
        ui._render_world.assert_not_called()
        ui._draw_hud.assert_not_called()
        for gear in ('D', 'N', 'P'):
            normal = state(gear_text=gear)
            ui.render(normal)
            ui._draw_hud.assert_called_once_with(normal, (False, False))
            ui._render_world.assert_called_once_with(normal, (False, False))
            ui._draw_hud.reset_mock()
            ui._render_world.reset_mock()
    assert draw.call_count == 20
    assert ui._reverse_camera.close.call_count == 60
