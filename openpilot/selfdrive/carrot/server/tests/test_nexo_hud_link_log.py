"""Focused tests for passive NEXO long-session external HUD diagnostics."""
from __future__ import annotations

import json

from openpilot.selfdrive.carrot.server.features.tools.nexo_hud_link_log import (
  HudLinkLog, classify_hud,
)


def sample(**updates):
  value = {
    "usb_interface": True,
    "usb_carrier": True,
    "jetson_connected": True,
    "display_error": "",
    "hud_enabled": True,
    "peer_age_s": 0.1,
    "hud_tx_age_s": 0.04,
    "hud_output_reported": None,
  }
  value.update(updates)
  return value


def test_hud_tx_does_not_prove_actual_display():
  assert classify_hud(sample()) == "TX_ACTIVE"
  assert classify_hud(sample(hud_output_reported=True)) == "HUD_CONFIRMED"
  assert classify_hud(sample(hud_tx_age_s=None)) == "AWAITING_HUD_TX"


def test_disconnected_and_deliberately_disabled_are_distinguished():
  assert classify_hud(sample(usb_carrier=False)) == "USB_LINK_DOWN"
  assert classify_hud(sample(usb_interface=False, usb_carrier=None)) == "USB_INTERFACE_MISSING"
  assert classify_hud(sample(jetson_connected=False, peer_age_s=4)) == "PEER_TIMEOUT"
  assert classify_hud(sample(hud_tx_age_s=4)) == "HUD_TX_TIMEOUT"
  assert classify_hud(sample(display_error="usb write failed")) == "DISPLAY_ERROR"
  assert classify_hud(sample(hud_enabled=False)) == "HUD_DISABLED"


def test_debounced_dropout_and_recovery_timeline(tmp_path):
  recorder = HudLinkLog(str(tmp_path))
  records = iter([
    sample(), sample(),
    sample(hud_tx_age_s=2), sample(hud_tx_age_s=2),
    sample(), sample(),
  ])
  recorder._capture = lambda now: {"classification": classify_hud(value := next(records)),
                                   **value}
  for now in (0.0, 0.6, 1.2, 1.8, 2.4, 3.0):
    recorder.tick(now)
  recorder.close()
  status = recorder.status()
  assert status["dropouts"] == 1
  assert status["recoveries"] == 1
  assert status["longest_outage_s"] == 1.2
  events = [json.loads(line) for line in (tmp_path / "hud_link_events.jsonl").read_text().splitlines()]
  assert [event["to"] for event in events] == ["TX_ACTIVE", "HUD_TX_TIMEOUT", "TX_ACTIVE"]
  assert events[1]["dropout"] is True
  assert events[2]["recovered_after_s"] == 1.2


def test_one_bad_sample_is_not_false_dropout(tmp_path):
  recorder = HudLinkLog(str(tmp_path))
  records = iter([sample(), sample(), sample(hud_tx_age_s=2), sample(), sample()])
  recorder._capture = lambda now: {"classification": classify_hud(value := next(records)), **value}
  for now in (0, 0.6, 1.2, 1.8, 2.4):
    recorder.tick(now)
  recorder.close()
  assert recorder.status()["dropouts"] == 0
