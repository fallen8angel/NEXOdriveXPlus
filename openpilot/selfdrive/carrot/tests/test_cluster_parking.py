import ast
from dataclasses import replace
from pathlib import Path
import math
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

CLUSTER = Path(__file__).resolve().parents[1] / "cluster"
sys.path.insert(0, str(CLUSTER))
from cluster_parking import (
    FRONT_LAYOUT,
    REAR_LAYOUT,
    NEXO_FINGERPRINT,
    NexoParkingTracker,
    ParkingIndications,
)
from cluster_scene import build_cluster_scene, cluster_scene_state_key
from test_cluster_blindspot_road import state


def frame(data, bus=0, address=0x4F4):
    return SimpleNamespace(dat=bytes.fromhex(data), src=bus, address=address)


def bits_for(layout, index, code):
    _, start, _ = layout[index]
    return (code << start).to_bytes(8, "little").hex()


class TestParkingDecoder(unittest.TestCase):
    def test_supplied_capture_examples_keep_observed_levels(self):
        tracker = NexoParkingTracker()
        expected = (
            ("0001401110100851", (0, 0, 0, 0, 0), (0, 1, 2, 2, 2)),
            ("0001401118100C71", (0, 0, 0, 0, 0), (0, 1, 2, 2, 3)),
            ("0021400040010101", (0, 0, 1, 1, 0), (0, 0, 0, 0, 0)),
            ("0025400040010101", (0, 1, 1, 1, 0), (0, 0, 0, 0, 0)),
            ("0045400080020201", (0, 1, 2, 2, 0), (0, 0, 0, 0, 0)),
            ("0029400080020201", (0, 2, 2, 1, 0), (0, 0, 0, 0, 0)),
        )
        for raw, front_codes, rear_codes in expected:
            with self.subTest(raw=raw):
                tracker.observe([frame(raw)], 10, 10)
                current = tracker.current(10)
                self.assertEqual(current.front_codes, front_codes)
                self.assertEqual(current.rear_codes, rear_codes)

    def test_dbc_bit_positions_cover_all_ten_indications(self):
        dbc = (CLUSTER.parents[3] / "opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc").read_text()
        block = dbc.split("BO_ 1268 SPAS12: 8 ")[1].split("BO_ ")[0]
        for signal, start, _ in FRONT_LAYOUT + REAR_LAYOUT:
            self.assertRegex(block, rf"SG_ CF_Spas_{signal}_Ind\s+: {start}\|3@1\+")

    def test_each_front_and_rear_position_keeps_levels_1_2_3(self):
        for end, layout in (("front", FRONT_LAYOUT), ("rear", REAR_LAYOUT)):
            for index in range(5):
                for code in (1, 2, 3):
                    tracker = NexoParkingTracker()
                    tracker.observe([frame(bits_for(layout, index, code))], 10, 10)
                    current = tracker.current(10)
                    codes = current.front_codes if end == "front" else current.rear_codes
                    self.assertEqual(codes[index], code)
                    parking = tracker.current_rear(10)
                    sensors = parking.front_sensors if end == "front" else parking.sensors
                    self.assertTrue(sensors[index].detected)
                    self.assertEqual(sensors[index].code, code)
                    self.assertEqual(sensors[index].proximity, ("far", "near", "very_near")[code - 1])

    def test_reserved_codes_are_not_displayed(self):
        for layout in (FRONT_LAYOUT, REAR_LAYOUT):
            for index in range(5):
                for value in (0, 4, 5, 6, 7):
                    tracker = NexoParkingTracker()
                    tracker.observe([frame(bits_for(layout, index, value))], 10, 10)
                    self.assertEqual(tracker.current(10), ParkingIndications())

    def test_echo_other_bus_and_pas11_do_not_create_warning(self):
        tracker = NexoParkingTracker()
        raw = bits_for(FRONT_LAYOUT, 1, 3)
        for bus in (1, 2, 128, 130, 192):
            tracker.observe([frame(raw, bus)], 10, 10)
        tracker.observe([frame(raw, address=0x436)], 10, 10)
        self.assertEqual(tracker.current(10), ParkingIndications())

    def test_timeout_invalid_and_short_frames_clear(self):
        tracker = NexoParkingTracker()
        tracker.observe([frame(bits_for(REAR_LAYOUT, 4, 3))], 10, 10)
        self.assertEqual(tracker.current(10).rear_codes[4], 3)
        self.assertEqual(tracker.current(11.01), ParkingIndications())
        tracker.observe([], 11, 11, valid=False)
        self.assertEqual(tracker.current(11), ParkingIndications())
        tracker.observe([SimpleNamespace(dat=b"\x00\x01", src=0, address=0x4F4)], 12, 12)
        self.assertEqual(tracker.current(12), ParkingIndications())


class TestParkingScene(unittest.TestCase):
    def test_each_position_and_level_has_independent_band_count_and_color(self):
        colors = {
            1: (55, 225, 83, 245),
            2: (255, 214, 40, 250),
            3: (255, 55, 48, 255),
        }
        for front in (True, False):
            for index in range(5):
                for code in (1, 2, 3):
                    codes = [0] * 5
                    codes[index] = code
                    indications = ParkingIndications(
                        front_codes=tuple(codes) if front else (0, 0, 0, 0, 0),
                        rear_codes=tuple(codes) if not front else (0, 0, 0, 0, 0),
                    )
                    scene = build_cluster_scene(state(parking_indications=indications))
                    self.assertEqual(len(scene.parking_warnings), code)
                    self.assertTrue(all(strip.color == colors[code] for strip in scene.parking_warnings))

    def test_front_sensor_levels_render_independently_in_drive_view(self):
        colors = {
            1: (55, 225, 83, 245),
            2: (255, 214, 40, 250),
            3: (255, 55, 48, 255),
        }
        for index in range(5):
            for code in (1, 2, 3):
                codes = [0] * 5
                codes[index] = code
                active = state(parking_indications=ParkingIndications(front_codes=tuple(codes)))
                scene = build_cluster_scene(active)
                self.assertEqual(len(scene.parking_warnings), code)
                self.assertTrue(all(strip.color == colors[code] for strip in scene.parking_warnings))

    def test_all_physical_points_remain_separate_in_drive_view(self):
        active = state(parking_indications=ParkingIndications(
            front_codes=(1, 1, 1, 1, 1),
            rear_codes=(1, 1, 1, 1, 1),
        ))
        # Five front + five rear physical positions, one far-stage strip each.
        self.assertEqual(len(build_cluster_scene(active).parking_warnings), 10)

    def test_drive_view_keeps_each_physical_stage_and_color(self):
        active = state(parking_indications=ParkingIndications(
            front_codes=(1, 3, 0, 2, 1),
            rear_codes=(0, 0, 0, 0, 0),
        ))
        scene = build_cluster_scene(active)
        # 1 + 3 + 0 + 2 + 1 stage bands stay independent.
        self.assertEqual(len(scene.parking_warnings), 7)
        colors = [strip.color for strip in scene.parking_warnings]
        self.assertEqual(colors.count((255, 55, 48, 255)), 3)
        self.assertEqual(colors.count((255, 214, 40, 250)), 2)
        self.assertEqual(colors.count((55, 225, 83, 245)), 2)

    def test_all_clear_cache_and_camera_view(self):
        clear = state()
        active = replace(clear, parking_indications=ParkingIndications(
            front_codes=(1, 2, 3, 2, 1),
            rear_codes=(1, 2, 3, 2, 1),
        ))
        self.assertEqual(build_cluster_scene(clear).parking_warnings, ())
        self.assertNotEqual(cluster_scene_state_key(clear), cluster_scene_state_key(active))
        from cluster_config import CLUSTER_CAMERA_VIEW_MODE_ROAD_CAMERA
        road_scene = build_cluster_scene(replace(active, camera_view_mode=CLUSTER_CAMERA_VIEW_MODE_ROAD_CAMERA))
        # Road-camera mode hides only the ego mesh; parking guidance must remain
        # available for projection over the live camera.
        self.assertEqual(len(road_scene.parking_warnings), 14)


class TestParkingLiveBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((CLUSTER / "cluster_live.py").read_text(encoding="utf-8"))
        source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OpenpilotLiveSource")
        method = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_with_parking_state")
        namespace = dict(NEXO_FINGERPRINT=NEXO_FINGERPRINT, ParkingIndications=ParkingIndications,
                         replace=replace, math=math, time=SimpleNamespace(monotonic=lambda: 10), ClusterUiState=object)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(CLUSTER / "cluster_live.py"), "exec"), namespace)
        cls.apply = namespace["_with_parking_state"]

    def source(self):
        event = SimpleNamespace(can=[frame("0025400040010101")], logMonoTime=10_000_000_000, valid=True)
        return SimpleNamespace(parser=SimpleNamespace(car_fingerprint=NEXO_FINGERPRINT),
                               _parking_tracker=NexoParkingTracker(), _parking_socket=None,
                               messaging=SimpleNamespace(sub_sock=Mock(return_value=object()),
                                                         recv_one_or_none=Mock(side_effect=[event, None])),
                               _service_alive=lambda _: True, _service_valid=lambda _: True)

    def test_receive_only_default_live_path_to_visible_mesh(self):
        source = self.source()
        # NEXO drive state is commonly displayed as the current gear step.
        updated = type(self).apply(source, state(onroad=True, speed_kph=2, gear_text="2"))
        source.messaging.sub_sock.assert_called_once_with("can", conflate=False)
        self.assertEqual(updated.parking_indications.front_codes, (0, 1, 1, 1, 0))
        self.assertEqual(len(build_cluster_scene(updated).parking_warnings), 3)

    def test_drive_gear_steps_and_d_label_show_parking(self):
        for gear_text in ("D", "1", "2", "8"):
            with self.subTest(gear_text=gear_text):
                source = self.source()
                updated = type(self).apply(source, state(onroad=True, speed_kph=2, gear_text=gear_text))
                self.assertEqual(updated.parking_indications.front_codes, (0, 1, 1, 1, 0))

    def test_invalid_offroad_and_non_drive_hide(self):
        for mode in ("invalid", "offroad", "park", "neutral"):
            source = self.source()
            if mode == "invalid":
                source._service_valid = lambda _: False
            gear_text = "P" if mode == "park" else "N" if mode == "neutral" else "2"
            updated = type(self).apply(source, state(onroad=mode != "offroad", speed_kph=0, gear_text=gear_text))
            self.assertEqual(updated.parking_indications, ParkingIndications())

    def test_drive_parking_is_not_hidden_by_speed(self):
        source = self.source()
        updated = type(self).apply(source, state(onroad=True, speed_kph=60, gear_text="6"))
        self.assertEqual(updated.parking_indications.front_codes, (0, 1, 1, 1, 0))

    def test_receive_failure_isolated_and_drain_is_bounded(self):
        source = self.source()
        source.messaging.recv_one_or_none.side_effect = OSError("disconnected")
        self.assertEqual(type(self).apply(source, state(onroad=True)).parking_indications, ParkingIndications())
        source = self.source()
        event = SimpleNamespace(can=[], logMonoTime=10_000_000_000, valid=True)
        source.messaging.recv_one_or_none = Mock(return_value=event)
        type(self).apply(source, state(onroad=True))
        self.assertEqual(source.messaging.recv_one_or_none.call_count, 64)


if __name__ == "__main__":
    unittest.main()
