"""Focused tests for passive NEXO long-session external HUD diagnostics."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import pytest

# Load the real passive module without booting unrelated web routes/native IPC.
spec = importlib.util.spec_from_file_location('hud_log_under_test',
  Path(__file__).parents[1] / 'features/tools/nexo_hud_link_log.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
HudLinkLog, classify_hud = module.HudLinkLog, module.classify_hud


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
  assert classify_hud(sample(hud_output_reported=True)) == "HUD_REPORTED"
  assert classify_hud(sample(hud_output_reported=False)) == "HUD_OUTPUT_DISCONNECTED"
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
  events = [event for line in (tmp_path / "hud_link_events.jsonl").read_text().splitlines()
            if (event := json.loads(line))["kind"] == "transition"]
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
  assert recorder.status()["transient_fault_observations"] == 1
  assert 'fault_observation' in (tmp_path / 'hud_link_events.jsonl').read_text()


@pytest.mark.parametrize('carrier,connected', [(None, False), (True, False)])
def test_missing_observation_is_not_a_disconnect(carrier, connected):
  assert classify_hud(sample(usb_carrier=carrier, jetson_connected=connected,
                            peer_age_s=None, hud_tx_age_s=None)) == 'AWAITING_HUD_TX'


def test_wifi_cannot_confirm_ncm_or_physical_output(tmp_path, monkeypatch):
  monkeypatch.setattr(module, 'read_record', lambda _: {})
  from openpilot.selfdrive.carrot.server.services import jetson
  monkeypatch.setattr(jetson, 'read_record', lambda _: {})
  recorder = HudLinkLog(tmp_path, {'channels': {'wifi': {'received_mono': 99.9,
      'packet': {'hud_connected': True, 'usb_connected': True}, 'packets': 1}}})
  monkeypatch.setattr(recorder, '_usb_carrier', lambda: (True, True))
  value = recorder._capture(100)
  recorder.close()
  assert not value['jetson_connected'] and value['hud_output_reported'] is None
  assert value['physical_hud_verified'] is False
  assert value['classification'] == 'AWAITING_HUD_TX'


def test_storage_failure_does_not_escape_tick(tmp_path):
  recorder = HudLinkLog(tmp_path)
  recorder._capture = lambda now: dict(sample(), classification='TX_ACTIVE')
  class Full:
    def write(self, raw):
      raise OSError(28, 'No space left on device')
    def flush(self):
      pass
    def close(self):
      pass
  recorder.samples_file.close()
  recorder.samples_file = Full()
  recorder.tick(0)
  recorder.tick(1)
  recorder.close()
  assert 'OSError' in recorder.status()['error']


def test_restart_preserves_previous_bytes_including_partial_record(tmp_path):
  path = tmp_path / 'hud_link_events.jsonl'
  path.write_bytes(b'{"kind":"old"}\n{"broken":')
  recorder = HudLinkLog(tmp_path)
  recorder.close()
  assert path.read_bytes() == b'{"kind":"old"}\n{"broken":\n'
  assert recorder.corrupt_records == 1


def test_source_event_survives_sub_sample_disconnect(tmp_path):
  recorder = HudLinkLog(tmp_path)
  recorder._capture = lambda now: dict(sample(), classification='TX_ACTIVE', ncm_events=[
      {'pid': 1, 'at': .1, 'state': 'retrying', 'error': 'peer closed'},
      {'pid': 1, 'at': .2, 'state': 'connected', 'error': ''}])
  for now in (0, .5, 1):
    recorder.tick(now)
  recorder.close()
  events = [json.loads(line) for line in Path(recorder.event_path).read_text().splitlines()]
  source = [v for v in events if v['kind'] == 'source_event']
  assert len(source) == 2 and source[0]['context_before']
  assert recorder.dropouts == 0  # source failures are not invented physical outages


def test_500_reconnections_keep_counts_and_memory_bounded(tmp_path):
  recorder = HudLinkLog(tmp_path)
  state = ['TX_ACTIVE']
  recorder._capture = lambda now: dict(sample(), classification=state[0])
  now = 0
  for _ in range(500):
    for state[0] in ('TX_ACTIVE', 'USB_LINK_DOWN', 'TX_ACTIVE'):
      for _ in range(2):
        recorder.tick(now)
        now += .5
  recorder.close()
  assert recorder.dropouts == recorder.recoveries == 500
  assert len(recorder.history) <= 32 and len(recorder.seen_events) <= 128
  assert recorder.status()['mean_recovery_s'] == 1.0
  assert recorder.error is None
