import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[4]
TOOLS = ROOT / "openpilot/selfdrive/carrot/server/features/tools"


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


sensor = load("nexo_sensor_test_module", TOOLS / "nexo_sensor_log.py")


def frame(address=0x58B, bus=0, data=b"\0" * 8):
  return types.SimpleNamespace(address=address, src=bus, dat=data)


class TestSensorLog(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.path = Path(self.tmp.name) / "sensor_events.jsonl"
    self.log = sensor.SensorLog(self.path, 0)
    self.addCleanup(self.log.close)

  def records(self):
    self.log.flush()
    return [json.loads(line) for line in self.path.read_text().splitlines()]

  def test_left_right_both_clear_and_context(self):
    for i, (left, right) in enumerate(((True, False), (False, True), (True, True), (False, False))):
      state = dict(leftBlindspot=left, rightBlindspot=right, leftBlinker=left,
                   rightBlinker=right, gearShifter="drive", vEgo=12.5, standstill=False)
      self.log.observe("carState", state, i, i + 10)
      self.log.observe("carState", state, i + 1, i + 11)
    rows = self.records()
    self.assertEqual(len(rows), 4)
    self.assertEqual([(r["data"]["leftBlindspot"], r["data"]["rightBlindspot"]) for r in rows],
                     [(True, False), (False, True), (True, True), (False, False)])
    self.assertEqual(rows[-1]["data"]["vEgo"], 12.5)
    self.assertEqual(rows[-1]["data"]["gearShifter"], "drive")
    self.assertFalse(rows[-1]["data"]["standstill"])

  def test_dbc_mapping_and_all_lca_values(self):
    dbc = (ROOT / "opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc").read_text()
    block = dbc.split("BO_ 1419 LCA11: 8 ")[1].split("BO_ ")[0]
    for side, start in (("Left", 8), ("Right", 16)):
      self.assertRegex(block, rf"SG_ CF_Lca_Ind{side}\s+: {start}\|2@1\+")
    for left in range(4):
      for right in range(4):
        data = bytes([0xFF, 0xFC | left, 0xFC | right, 0, 0, 0, 0, 0])
        self.log.observe("can", [frame(data=data)], left * 4 + right, 42)
    self.assertEqual([(r["CF_Lca_IndLeft"], r["CF_Lca_IndRight"]) for r in self.records()],
                     [(l, r) for l in range(4) for r in range(4)])

  def test_parking_changes_bus_and_receive_time(self):
    for address in (0x4F4, 0x436):
      for bus in (0, 1, 2):
        for i in (0, 0, 1):
          self.log.observe("can", [frame(address, bus, bytes([i, 0, 0, 0]))], 100 + i, 200 + i)
    rows = self.records()
    self.assertEqual(len(rows), 12)
    self.assertEqual({r["bus"] for r in rows}, {0, 1, 2})
    self.assertEqual(rows[-1]["dataHex"], "01000000")
    self.assertEqual(rows[-1]["receiveMonoNs"], 101)
    self.assertEqual(rows[-1]["logMonoTime"], 201)
    self.assertIn("do not prove sensors are off", self.log.status()["notes"])

  def test_periodic_missing_and_stale_values(self):
    self.log.tick(sensor.PERIOD_NS - 1)
    self.assertEqual(self.records(), [])
    self.log.tick(sensor.PERIOD_NS)
    self.assertIsNone(self.records()[0]["carState"])
    self.log.observe("carState", {"leftBlindspot": False}, 6_000_000_000, 55, False)
    self.log.observe("can", [frame(data=b"\x00\x01")], 6_000_000_001, 56)
    self.log.tick(10_000_000_000)
    row = self.records()[-1]
    self.assertEqual(row["carState"]["receiveMonoNs"], 6_000_000_000)
    self.assertIsNone(row["carState"]["data"]["rightBlindspot"])
    self.assertFalse(row["carState"]["valid"])
    self.assertIsNone(row["can"][0]["CF_Lca_IndLeft"])
    self.assertEqual(row["can"][0]["dataHex"], "0001")

  def test_limit_preserves_complete_lines(self):
    self.log.max_bytes = 200
    self.log.tick(sensor.PERIOD_NS)
    self.log.observe("carState", {}, 6_000_000_000, 1)
    self.log.flush()
    self.assertTrue(self.log.limit_reached)
    self.assertLessEqual(self.path.stat().st_size, 200)
    self.assertEqual(len(self.records()), 1)
    self.assertEqual(sensor.MAX_BYTES, 134217728)

  def test_open_failure(self):
    failed = sensor.SensorLog(self.path / "missing", 0)
    failed.observe("carState", {}, 1, 1)
    failed.tick(sensor.PERIOD_NS)
    failed.close()
    self.assertIsNotNone(failed.status()["error"])

  def test_write_flush_close_failures(self):
    self.log.close()
    for method in ("write", "flush", "close"):
      with self.subTest(method=method):
        self.log.error = None
        self.log.file = Mock()
        self.log.file.write.side_effect = lambda data: len(data)
        getattr(self.log.file, method).side_effect = OSError("disk failure")
        self.log.observe("carState", {"vEgo": len(method)}, 1, 1)
        self.log.close()
        self.assertIn("disk failure", self.log.error)


class TestLongLoggerIntegration(unittest.TestCase):
  def test_raw_collection_report_and_archive_survive_sensor_failure(self):
    for fail in (None, "open", "write", "limit"):
      with self.subTest(sensor_failure=fail), tempfile.TemporaryDirectory() as tmp:
        modules = {
          "openpilot.cereal": types.SimpleNamespace(log=Mock(), messaging=Mock()),
          "openpilot.cereal.services": types.SimpleNamespace(SERVICE_LIST={"can": 1, "carState": 1}),
          "openpilot.common.params": types.SimpleNamespace(Params=Mock()),
          "openpilot.selfdrive.carrot.server.features.tools.nexo_sensor_log": sensor,
        }
        with patch.dict(sys.modules, modules):
          logger = load("nexo_long_test_module", TOOLS / "nexo_long_logger.py")
        logger.STATE_PATH = str(Path(tmp) / "state.json")
        logger.LATEST_ARCHIVE = str(Path(tmp) / "result.tar.gz")
        session = Path(tmp) / "session"
        session.mkdir()
        messages = {}
        sockets = {}
        for service in ("can", "carState"):
          raw = service.encode()
          msg = Mock()
          msg.which.return_value = service
          msg.logMonoTime = 123
          msg.valid = True
          setattr(msg, service, [frame()] if service == "can" else {"leftBlindspot": True, "rightBlindspot": False})
          context = Mock()
          context.__enter__ = Mock(return_value=msg)
          context.__exit__ = Mock(return_value=False)
          messages[raw] = context
          sock = Mock()
          sock.receive.side_effect = [raw, None]
          sockets[service] = sock
        logger.messaging.sub_sock.side_effect = lambda service, **kwargs: sockets[service]
        logger.messaging.Poller.return_value.poll.return_value = list(sockets.values())
        logger.log.Event.from_bytes.side_effect = lambda raw, **kwargs: messages[raw]
        logger._stop_event = Mock()
        logger._stop_event.is_set.return_value = True
        logger._git_snapshot = Mock(return_value={})
        logger._param_snapshot = Mock()
        logger._source_scan = Mock()
        logger._route_log_references = Mock(return_value=[])
        original_open = open

        def sensor_open(path, *args, **kwargs):
          if fail == "open":
            raise OSError("sensor-only failure")
          return original_open(path, *args, **kwargs)

        def make_sensor(path, start_ns):
          result = sensor.SensorLog(path, start_ns, max_bytes=0 if fail == "limit" else sensor.MAX_BYTES)
          if fail == "write":
            result.file.close()
            result.file = Mock()
            result.file.write.side_effect = OSError("sensor-only failure")
          return result

        logger.SensorLog = make_sensor
        with patch.object(sensor, "open", sensor_open, create=True):
          logger._worker_main("test", str(session), 0, 0)
        raw = (session / "raw_events.bin").read_bytes()
        self.assertTrue(raw.startswith(logger._MAGIC))
        offset = len(logger._MAGIC)
        payloads = []
        while offset < len(raw):
          _, _, length = logger._RECORD.unpack_from(raw, offset)
          offset += logger._RECORD.size
          payloads.append(raw[offset:offset + length])
          offset += length
        self.assertEqual(payloads, [b"can", b"carState"])
        self.assertIn("0x58B", (session / "can_rx.csv").read_text())
        self.assertTrue(json.loads((session / "events.jsonl").read_text())["data"]["leftBlindspot"])
        report = (session / "report.txt").read_text(encoding="utf-8")
        self.assertIn("sensor_events.jsonl", report)
        self.assertIn("independently of any full-log limit", report)
        self.assertIn("NEXO_LONG_LOG_COMPLETE", report)
        status = json.loads((session / "manifest.json").read_text())["sensor_index"]
        if fail in ("open", "write"):
          self.assertIn("sensor-only failure", report)
          self.assertIn("sensor-only failure", status["error"])
        else:
          with tarfile.open(logger.LATEST_ARCHIVE) as archive:
            self.assertIn("nexo-long-log-test/sensor_events.jsonl", archive.getnames())
        if fail == "limit":
          self.assertTrue(status["limit_reached"])
          self.assertIn('"limit_reached": true', report)
        self.assertTrue(logger._state["finished"])


if __name__ == "__main__":
  unittest.main()
