from dataclasses import replace
from unittest.mock import Mock

from cluster_renderer import ClusterUiRenderer
from cluster_scene import build_cluster_scene
from test_cluster_blindspot_road import state


def renderer():
    result = ClusterUiRenderer.__new__(ClusterUiRenderer)
    result._draw_quad = Mock()
    result._draw_vehicle_shadow = Mock()
    return result


def test_brake_lights_change_only_lamp_colors_and_release():
    ui = renderer()
    off = build_cluster_scene(state()).vehicles[0]
    on = build_cluster_scene(state(brake_lights=True)).vehicles[0]
    frames = []
    for vehicle in (off, on, off):
        ui._draw_quad.reset_mock()
        ui._draw_vehicle(vehicle)
        frames.append([call.args for call in ui._draw_quad.call_args_list])
    assert frames[0] == frames[2]
    assert [face[:4] for face in frames[0]] == [face[:4] for face in frames[1]]
    changes = [(a, b) for a, b in zip(frames[0], frames[1]) if a != b]
    # Two-sided faces: both triangular lamps, both insets, and the center lamp.
    assert len(changes) == 10
    assert all(b[-1] in ((255, 38, 35, 255), (255, 89, 64, 255)) for a, b in changes)


def test_detected_vehicle_still_uses_existing_model():
    ui = renderer()
    ui._vehicle_model = object()
    ui._draw_vehicle_model = Mock()
    ui._draw_nexo_ego = Mock()
    vehicle = replace(build_cluster_scene(state()).vehicles[0], source='camera', primary=True)
    ui._draw_vehicle(vehicle)
    ui._draw_vehicle_model.assert_called_once_with(vehicle)
    ui._draw_nexo_ego.assert_not_called()


def test_mesh_follows_vehicle_heading():
    ui = renderer()
    vehicle = build_cluster_scene(state()).vehicles[0]
    ui._draw_vehicle(vehicle)
    initial = [c.args for c in ui._draw_quad.call_args_list]
    ui._draw_quad.reset_mock()
    rotated = replace(vehicle, right_x=-vehicle.right_y, right_y=vehicle.right_x,
                      forward_x=-vehicle.forward_y, forward_y=vehicle.forward_x)
    ui._draw_vehicle(rotated)
    for a, b in zip(initial, [c.args for c in ui._draw_quad.call_args_list]):
        for p, q in zip(a[:4], b[:4]):
            assert abs(q.x - (vehicle.center.x - (p.y - vehicle.center.y))) < 1e-8
            assert abs(q.y - (vehicle.center.y + (p.x - vehicle.center.x))) < 1e-8
            assert q.z == p.z
