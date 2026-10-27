"""Passive long-session HUD link telemetry. Never connects, sends, or alters vehicle state."""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
import json
import os
import time

from openpilot.selfdrive.carrot.server.services.jetson import (
  NCM_DISPLAY_STATUS, DISPLAY_STATUS, LINK_STATUS, age, fresh, read_record, snapshot,
)

SAMPLE_INTERVAL_S = 0.5
PERIODIC_INTERVAL_S = 2.0
HUD_TX_TIMEOUT_S = 1.5
PEER_TIMEOUT_S = 3.0
MAX_SAMPLES_BYTES = 32 * 1024 * 1024
MAX_EVENTS_BYTES = 8 * 1024 * 1024
ACTIVE = frozenset(("TX_ACTIVE", "HUD_REPORTED"))
FAULTS = frozenset(("USB_LINK_DOWN", "USB_INTERFACE_MISSING", "PEER_TIMEOUT",
                    "HUD_TX_TIMEOUT", "HUD_OUTPUT_DISCONNECTED", "DISPLAY_ERROR", "NCM_SESSION_ERROR"))


def _limited(value, width=180):
  return str(value or "")[:width]


def classify_hud(sample):
  """Evidence classes, not hardware-fault diagnoses. No HUD RX proof means TX only."""
  if sample["hud_enabled"] is False:
    return "HUD_DISABLED"
  if sample["usb_carrier"] is False:
    return "USB_LINK_DOWN"
  if sample["usb_interface"] is False:
    return "USB_INTERFACE_MISSING"
  if sample["display_error"]:
    return "DISPLAY_ERROR"
  if sample.get("ncm_state") in ("retrying", "disconnected"):
    return "NCM_SESSION_ERROR"
  if sample["peer_age_s"] is not None and sample["peer_age_s"] >= PEER_TIMEOUT_S:
    return "PEER_TIMEOUT"
  tx = sample["hud_tx_age_s"]
  if tx is not None and tx >= HUD_TX_TIMEOUT_S:
    return "HUD_TX_TIMEOUT"
  if tx is not None and tx < HUD_TX_TIMEOUT_S:
    if sample["hud_output_reported"] is False:
      return "HUD_OUTPUT_DISCONNECTED"
    return "HUD_REPORTED" if sample["hud_output_reported"] is True else "TX_ACTIVE"
  return "AWAITING_HUD_TX"


class HudLinkLog:
  def __init__(self, session_dir, jetson_state=None):
    self.jetson_state = jetson_state
    self.sample_path = os.path.join(session_dir, "hud_link_samples.jsonl")
    self.event_path = os.path.join(session_dir, "hud_link_events.jsonl")
    self.samples_file = None
    self.events_file = None
    self.sample_bytes = 0
    self.event_bytes = 0
    self.sample_count = 0
    self.event_count = 0
    self.sample_limit = False
    self.event_limit = False
    self.error = None
    self.dropouts = 0
    self.recoveries = 0
    self.longest_outage_s = 0.0
    self.outage_start = None
    self.reason_counts = Counter()
    self.last_state = None
    self.pending_state = None
    self.pending_count = 0
    self.last_tick = float("-inf")
    self.last_periodic = float("-inf")
    self.latest = {}
    self.history = deque(maxlen=32)
    self.seen_events = deque(maxlen=128)
    self.context_until = float('-inf')
    self.recovery_total_s = 0.0
    self.transient_observations = 0
    self.corrupt_records = 0
    try:
      # A restarted monitor must never truncate previously collected evidence.
      self.sample_bytes = os.path.getsize(self.sample_path) if os.path.exists(self.sample_path) else 0
      self.event_bytes = os.path.getsize(self.event_path) if os.path.exists(self.event_path) else 0
      self.samples_file = open(self.sample_path, "ab")
      self.events_file = open(self.event_path, "ab")
      if self.event_bytes:
        last_line = b''
        with open(self.event_path, 'rb') as previous:
          while line := previous.readline(MAX_EVENTS_BYTES + 1):
            last_line = line
            try:
              record = json.loads(line)
              if not isinstance(record, dict):
                raise ValueError('expected object')
              if record.get('kind') == 'transition':
                if record.get('dropout') is True:
                  self.dropouts += 1
                  self.reason_counts[str(record.get('to'))] += 1
                gap = record.get('recovered_after_s')
                if type(gap) in (int, float) and 0 <= gap < 1e8:
                  self.recoveries += 1
                  self.recovery_total_s += gap
                  self.longest_outage_s = max(self.longest_outage_s, gap)
            except (ValueError, TypeError):
              self.corrupt_records += 1
        # Preserve a truncated tail as evidence, but isolate the next JSON record.
        if not last_line.endswith(b'\n'):
          self.events_file.write(b'\n')
          self.event_bytes += 1
    except OSError as e:
      self.error = f"{type(e).__name__}: {_limited(e)}"

  @staticmethod
  def _usb_carrier():
    from openpilot.selfdrive.modeld.jetlink.ncm_transport import comma_interface, NET_CLASS
    try:
      interface = NET_CLASS / comma_interface()
      text = (interface / "carrier").read_text(encoding="utf-8").strip()
      return True, True if text == "1" else False if text == "0" else None
    except OSError:
      # No unique observable bound NCM function is not proof of physical loss.
      return None, None

  @staticmethod
  def _age_seconds(value, now):
    delta = age(value, now)
    return round(delta, 3) if delta is not None else None

  def _capture(self, now):
    # Copy each immutable datagram entry once, avoiding iteration over an asyncio-mutated dict.
    state = self.jetson_state if isinstance(self.jetson_state, dict) else {}
    channels = {k: v for k, v in dict(state.get("channels") or {}).items()
                if isinstance(v, dict) and isinstance(v.get("packet"), dict)}
    # Wi-Fi beacons are retained as independent observations, never NCM proof.
    current = snapshot({"channels": {k: v for k, v in channels.items() if k == "usb"}}, now=now)
    ncm = read_record(NCM_DISPLAY_STATUS)
    display = read_record(DISPLAY_STATUS)
    link = next((entry for path in LINK_STATUS if fresh(entry := read_record(path), now)), {})
    interface, carrier = self._usb_carrier()
    ncm_fresh = fresh(ncm, now, 2.0)
    display_fresh = fresh(display, now)
    peer_ages = [now - entry["received_mono"] for entry in channels.values()
                 if isinstance(entry, dict) and isinstance(entry.get("received_mono"), (float, int))
                 and 0 <= now - entry["received_mono"]]
    status = current.get("status") or {}
    recent = current.get("recent") or {}
    stability = current.get("stability") or {}
    peers = status.get("host_telemetry") or {}
    if isinstance(peers, dict):
      peers = peers.get("telemetry", peers)
    if not isinstance(peers, dict):
      peers = {}
    hud_rx = peers.get("carrot_hud_connected")
    if type(hud_rx) is not bool:
      hud_rx = status.get("hud_connected") if type(status.get("hud_connected")) is bool else None
    tx_age = recent.get("hud_tx_age_s")
    if tx_age is None and ncm_fresh:
      tx_age = age(ncm.get("last_hud_tx_mono"), now)
    if tx_age is None and link:
      tx_age = age(link.get("last_hud_tx_mono"), now)
    heartbeat_age = recent.get("heartbeat_age_s")
    display_error = (ncm.get("display_error") if ncm_fresh else None) or (
      display.get("display_error") if display_fresh else None) or status.get("display_error")
    hud_enabled = ncm.get("hud_enabled") if ncm_fresh else link.get("hud_enabled") if link else None
    stages = {v.get("id"): v.get("state") for v in current.get("stages", []) if isinstance(v, dict)}
    wall = time.time()  # noqa: TID251 - wall time labels; durations use monotonic time
    sample = {
      "time_local": datetime.fromtimestamp(wall).astimezone().isoformat(timespec="milliseconds"),
      "time_unix_ms": round(wall * 1000),
      "monotonic_ns": time.monotonic_ns(),
      "usb_interface": interface, "usb_carrier": carrier,
      "jetson_connected": current.get("connected") is True,
      "jetson_state": _limited(current.get("state"), 70),
      "peer_age_s": round(heartbeat_age, 3) if isinstance(heartbeat_age, (float, int)) else None,
      "udp_beacon_age_s": round(min(peer_ages), 3) if peer_ages else None,
      "usb_beacon_count": int((channels.get("usb") or {}).get("packets") or 0),
      "wifi_beacon_count": int((channels.get("wifi") or {}).get("packets") or 0),
      "ncm_fresh": ncm_fresh, "ncm_state": _limited(ncm.get("state") if ncm_fresh else "", 60),
      "ncm_capability": ncm.get("capability_negotiated") is True if ncm_fresh else None,
      "jetlink_state": _limited(link.get("state"), 60),
      "hud_tx_age_s": round(tx_age, 3) if isinstance(tx_age, (float, int)) else None,
      "hud_bytes": ncm.get("hud_bytes") if ncm_fresh and type(ncm.get("hud_bytes")) is int else None,
      "hud_enabled": hud_enabled if type(hud_enabled) is bool else None,
      "hud_output_reported": hud_rx if type(hud_rx) is bool else None,
      "physical_hud_verified": False,
      "physical_hud_status": "확인 불가",
      "ncm_events": ncm.get("diagnostic_events", []) if ncm_fresh else [],
      "ncm_pid": ncm.get("pid") if ncm_fresh else None,
      "ncm_interface": _limited(ncm.get("interface"), 32) if ncm_fresh else "",
      "hud_rx_confirmed": ncm.get("hud_rx_confirmed") if ncm_fresh else None,
      "hud_pipeline": (ncm.get("peer") or {}).get("hud_pipeline", {})
                      if ncm_fresh and isinstance(ncm.get("peer"), dict) else {},
      "hud_send_ms": ncm.get("hud_send_ms") if ncm_fresh else None,
      "hud_produced_mono": ncm.get("hud_produced_mono") if ncm_fresh else None,
      "hud_tx_mono": ncm.get("last_hud_tx_mono") if ncm_fresh else None,
      "hud_data_received_reported": peers.get("hud_data_received") if type(peers.get("hud_data_received")) is bool else None,
      "display_error": _limited(display_error),
      "jetson_last_error": _limited(status.get("last_error")),
      "hud_recovery": stability.get("hud_recovery") if isinstance(stability.get("hud_recovery"), dict) else {},
      "stages": stages,
    }
    sample["classification"] = classify_hud(sample)
    return sample

  def _write(self, handle, record, maximum, kind):
    if self.error or handle is None:
      return
    data = (json.dumps(record, ensure_ascii=False, allow_nan=False, default=str) + "\n").encode("utf-8")
    count = self.sample_bytes if kind == "sample" else self.event_bytes
    if count + len(data) > maximum:
      if kind == "sample":
        self.sample_limit = True
      else:
        self.event_limit = True
      return
    if handle.write(data) != len(data):
      raise OSError("short HUD diagnostics write")
    if kind == "sample":
      self.sample_bytes += len(data)
      self.sample_count += 1
    else:
      self.event_bytes += len(data)
      self.event_count += 1

  def tick(self, now=None):
    now = time.monotonic() if now is None else now
    if now - self.last_tick < SAMPLE_INTERVAL_S:
      return
    self.last_tick = now
    if self.error:
      return
    try:
      item = self._capture(now)
      self.latest = item
      new = item["classification"]
      events = item.get("ncm_events", [])
      for observed in events[-32:] if isinstance(events, list) else []:
        if not isinstance(observed, dict):
          continue
        key = (observed.get("pid"), observed.get("at"), observed.get("state"))
        delta = age(observed.get("at"), now)
        if delta is None or delta > 15 or key in self.seen_events:
          continue
        self.seen_events.append(key)
        self._write(self.events_file, {"kind": "source_event", "evidence": observed,
                    "context_before": list(self.history), "physical_hud_verified": False}, MAX_EVENTS_BYTES, "event")
        self.context_until = now + 15
      if new in FAULTS and new != self.pending_state:
        self.transient_observations += 1
        self._write(self.events_file, {"kind": "fault_observation", **item,
                    "context_before": list(self.history)}, MAX_EVENTS_BYTES, "event")
        self.context_until = now + 15
      if now <= self.context_until:
        self._write(self.samples_file, {"kind": "incident_context", **item}, MAX_SAMPLES_BYTES, "sample")
      self.history.append({k: v for k, v in item.items() if k != "ncm_events"})
      if now - self.last_periodic >= PERIODIC_INTERVAL_S and not self.sample_limit:
        self._write(self.samples_file, {"kind": "periodic", **item}, MAX_SAMPLES_BYTES, "sample")
        self.last_periodic = now
      if new != self.pending_state:
        self.pending_state, self.pending_count = new, 1
      else:
        self.pending_count += 1
      if self.pending_count >= 2 and new != self.last_state:
        previous = self.last_state
        event = {"kind": "transition", "from": previous, "to": new, **item}
        if previous in ACTIVE and new in FAULTS:
          self.dropouts += 1
          self.reason_counts[new] += 1
          self.outage_start = now
          event["dropout"] = True
        elif self.outage_start is not None and new in ACTIVE:
          gap = round(max(0.0, now - self.outage_start), 2)
          self.recoveries += 1
          self.recovery_total_s += gap
          self.longest_outage_s = max(self.longest_outage_s, gap)
          self.outage_start = None
          event["recovered_after_s"] = gap
        self._write(self.events_file, event, MAX_EVENTS_BYTES, "event") if not self.event_limit else None
        self.last_state = new
    except Exception as e:
      # HUD diagnostics must never interrupt the existing vehicle/CAN recorder.
      self.error = f"{type(e).__name__}: {_limited(e)}"

  def flush(self):
    try:
      for handle in (self.samples_file, self.events_file):
        if handle is not None:
          handle.flush()
    except OSError as e:
      self.error = f"{type(e).__name__}: {_limited(e)}"

  def close(self):
    self.flush()
    for handle in (self.samples_file, self.events_file):
      if handle is not None:
        try:
          handle.close()
        except OSError as e:
          self.error = f"{type(e).__name__}: {_limited(e)}"

  def status(self):
    return {
      "samples_file": "hud_link_samples.jsonl", "events_file": "hud_link_events.jsonl",
      "sample_count": self.sample_count, "event_count": self.event_count,
      "dropouts": self.dropouts, "recoveries": self.recoveries,
      "longest_outage_s": self.longest_outage_s,
      "mean_recovery_s": round(self.recovery_total_s / self.recoveries, 3) if self.recoveries else None,
      "unresolved_outages": int(self.outage_start is not None),
      "automatic_recovery_successes": None,
      "automatic_recovery_failures": None,
      "transient_fault_observations": self.transient_observations,
      "physical_hud_verified": False,
      "cause_confirmed": False,
      "corrupt_records": self.corrupt_records,
      "ongoing_outage_s": round(max(0, time.monotonic() - self.outage_start), 2) if self.outage_start else None,
      "failure_reasons": dict(self.reason_counts),
      "latest": self.latest, "sample_limit_reached": self.sample_limit,
      "event_limit_reached": self.event_limit, "error": self.error,
      "note": "TX_ACTIVE=송신, HUD_REPORTED=소프트웨어 보고. 실제 물리 출력·근본 원인·자동 복구 성공/실패는 확인 불가. " +
              "0.5초 관측, 2회 전환 확인. 단일 이상 관측과 NCM 오류 이벤트는 별도 기록하며 연결 끊김 횟수와 구분합니다. " +
              "오류 전후 최대 15초 표본을 보존합니다. 샘플 사이의 물리적 끊김은 관측되지 않을 수 있습니다.",
    }
