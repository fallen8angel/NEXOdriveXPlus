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
from cluster_parking import NEXO_FINGERPRINT, NexoParkingTracker, ParkingIndications
from cluster_scene import build_cluster_scene, cluster_scene_state_key
from test_cluster_blindspot_road import state


def frame(data, bus=0, address=0x4F4):
    return SimpleNamespace(dat=bytes.fromhex(data), src=bus, address=address)


class TestParkingDecoder(unittest.TestCase):
    def test_supplied_capture_examples(self):
        tracker = NexoParkingTracker()
        for raw, expected in (
            ("0001401110100851", (False, False, True, True)),
            ("0001401118100C71", (False, False, True, True)),
            ("0021400040010101", (False, True, False, False)),
            ("0025400040010101", (True, True, False, False)),
            ("0045400080020201", (True, True, False, False)),
            ("0029400080020201", (True, True, False, False)),
            ("0001000000003000", (False, False, False, False)),
            ("0000000000000000", (False, False, False, False)),
        ):
            with self.subTest(raw=raw):
                tracker.observe([frame(raw)], 10, 10)
                self.assertEqual(tracker.current(10), ParkingIndications(*expected))

    def test_dbc_bit_positions(self):
        dbc = (CLUSTER.parents[3] / "opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc").read_text()
        block = dbc.split("BO_ 1268 SPAS12: 8 ")[1].split("BO_ ")[0]
        for signal, start in (("FIL", 10), ("FIR", 13), ("RIL", 24), ("RIR", 27), ("ROR", 35)):
            self.assertRegex(block, rf"SG_ CF_Spas_{signal}_Ind\s+: {start}\|3@1\+")

    def test_reserved_codes_and_unobserved_positions_are_not_used(self):
        for start in (10, 13, 24, 27, 35):
            for value in (0, 4, 5, 6, 7):
                tracker = NexoParkingTracker()
                data = (value << start).to_bytes(8, "little").hex()
                tracker.observe([frame(data)], 10, 10)
                self.assertEqual(tracker.current(10), ParkingIndications())
        for start in (16, 19, 32):
            tracker.observe([frame((1 << start).to_bytes(8, "little").hex())], 10, 10)
            self.assertEqual(tracker.current(10), ParkingIndications())

    def test_echo_other_bus_and_pas11_do_not_create_warning(self):
        tracker = NexoParkingTracker()
        for bus in (1, 2, 128, 130, 192):
            tracker.observe([frame("0025400040010101", bus)], 10, 10)
        tracker.observe([frame("0025400040010101", address=0x436)], 10, 10)
        self.assertEqual(tracker.current(10), ParkingIndications())

    def test_timeout_zero_invalid_and_short_frames_clear(self):
        for invalid in ("timeout", "zero", "invalid", "short"):
            tracker = NexoParkingTracker()
            tracker.observe([frame("0025400040010101")], 10, 10)
            self.assertTrue(tracker.current(10).front_left)
            if invalid == "zero":
                tracker.observe([frame("0000000000000000")], 10.1, 10.1)
            elif invalid == "invalid":
                tracker.observe([], 10.1, 10.1, False)
            elif invalid == "short":
                tracker.observe([frame("0025")], 10.1, 10.1)
            self.assertEqual(tracker.current(11.01 if invalid == "timeout" else 10.1), ParkingIndications())

    def test_delayed_future_or_out_of_order_data_does_not_revive_warning(self):
        tracker = NexoParkingTracker()
        for stamp in (8, 11, float("nan")):
            tracker.observe([frame("0025400040010101")], stamp, 10)
            self.assertEqual(tracker.current(10), ParkingIndications())
        tracker.observe([frame("0000000000000000")], 10, 10)
        tracker.observe([frame("0025400040010101")], 9.9, 10)
        self.assertEqual(tracker.current(10), ParkingIndications())


class TestParkingScene(unittest.TestCase):
    def test_fans_stay_on_correct_bumper_corner(self):
        for index in range(4):
            fields = [False] * 4
            fields[index] = True
            value = state(parking_indications=ParkingIndications(*fields))
            scene = build_cluster_scene(value)
            ego = scene.vehicles[0]
            self.assertEqual(len(scene.parking_warnings), 3)
            side, direction = (-1 if index % 2 == 0 else 1), (1 if index < 2 else -1)
            for strip in scene.parking_warnings:
                self.assertEqual(strip.color, (255, 180, 0, 210))
                for p in strip.left + strip.right:
                    dx, dy = p.x - ego.center.x, p.y - ego.center.y
                    self.assertGreater(side * (dx * ego.right_x + dy * ego.right_y), 0)
                    self.assertGreater(direction * (dx * ego.forward_x + dy * ego.forward_y), ego.length_m / 2)
                a, b, c = strip.left[0], strip.right[0], strip.right[1]
                self.assertGreater((b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x), 0)

    def test_all_clear_cache_and_camera_view(self):
        clear = state()
        active = replace(clear, parking_indications=ParkingIndications(True, True, True, True))
        self.assertEqual(len(build_cluster_scene(active).parking_warnings), 12)
        self.assertEqual(build_cluster_scene(clear).parking_warnings, ())
        self.assertNotEqual(cluster_scene_state_key(clear), cluster_scene_state_key(active))
        from cluster_config import CLUSTER_CAMERA_VIEW_MODE_ROAD_CAMERA
        self.assertEqual(build_cluster_scene(replace(active, camera_view_mode=CLUSTER_CAMERA_VIEW_MODE_ROAD_CAMERA)).parking_warnings, ())


class TestParkingLiveBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the actual live method without cereal/GPU dependencies on desktop.
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
        updated = type(self).apply(source, state(onroad=True, speed_kph=2))
        source.messaging.sub_sock.assert_called_once_with("can", conflate=False)
        self.assertEqual(len(build_cluster_scene(updated).parking_warnings), 6)

    def test_ineligible_car_invalid_state_offroad_and_speed_hide(self):
        for mode in ("other_car", "invalid", "offroad", "fast"):
            source = self.source()
            if mode == "other_car":
                source.parser.car_fingerprint = "OTHER"
            if mode == "invalid":
                source._service_valid = lambda _: False
            updated = type(self).apply(source, state(onroad=mode != "offroad", speed_kph=16 if mode == "fast" else 0))
            self.assertEqual(updated.parking_indications, ParkingIndications())

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
