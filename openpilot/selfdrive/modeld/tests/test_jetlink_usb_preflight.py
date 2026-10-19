"""Regression tests for read-only AGNOS gadget preflight.

No tests bind a controller, change a vehicle parameter, or open a USB device.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openpilot.selfdrive.modeld.jetlink.usb_preflight import snapshot, format_lines


class TestUsbPreflight(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.gadgets = self.root / "usb_gadget"
    self.udcs = self.root / "udc"
    self.usb0 = self.root / "net" / "usb0"
    self.gadgets.mkdir()
    self.udcs.mkdir()
    self.usb0.mkdir(parents=True)
    (self.udcs / "a600000.dwc3").mkdir()
    (self.udcs / "a600000.dwc3" / "state").write_text("configured\n")
    (self.usb0 / "carrier").write_text("1\n")
    (self.usb0 / "operstate").write_text("up\n")

  def gadget(self, name: str, owner: str, functions=()):
    base = self.gadgets / name
    (base / "functions").mkdir(parents=True)
    (base / "UDC").write_text(owner + "\n")
    for function in functions:
      (base / "functions" / function).mkdir()

  def collect(self):
    return snapshot(configfs=self.gadgets, udc_root=self.udcs, usb_net=self.usb0)

  def test_agnos_g1_with_adb_ncm_is_blocker_not_jetlink_ready(self):
    self.gadget("g1", "a600000.dwc3", ("ffs.adb", "ncm.0"))
    data = self.collect()
    self.assertEqual(data["verdict"], "agnos_g1_busy")
    self.assertEqual(data["controllers"][0]["state"], "configured")
    self.assertEqual(data["usb0"]["carrier"], True)
    self.assertEqual(data["owners"][0]["functions"], ["ffs.adb", "ncm.0"])
    self.assertTrue(any("추론 성공이 아닙니다" in line for line in format_lines(data)))

  def test_unbound_g1_is_not_blocking(self):
    self.gadget("g1", "", ("ffs.adb", "ncm.0"))
    self.assertEqual(self.collect()["verdict"], "udc_available")

  def test_jetlink_bound_does_not_claim_host_model_ready(self):
    self.gadget("jetlink", "a600000.dwc3", ("ffs.jetlink",))
    result = self.collect()
    self.assertEqual(result["verdict"], "jetlink_bound")
    self.assertIn("별도로 확인", result["reason"])

  def test_other_gadget_owner_blocks_jetlink(self):
    self.gadget("some_other_owner", "a600000.dwc3", ("acm.GS0",))
    self.assertEqual(self.collect()["verdict"], "other_gadget_busy")

  def test_no_udc(self):
    (self.udcs / "a600000.dwc3" / "state").unlink()
    (self.udcs / "a600000.dwc3").rmdir()
    self.assertEqual(self.collect()["verdict"], "no_udc")

  def test_missing_usb0_is_unknown_not_assumed_disconnected(self):
    (self.usb0 / "carrier").unlink()
    (self.usb0 / "operstate").unlink()
    self.usb0.rmdir()
    self.assertEqual(self.collect()["usb0"]["carrier"], None)
    self.assertFalse(self.collect()["usb0"]["present"])


if __name__ == "__main__":
  unittest.main()
