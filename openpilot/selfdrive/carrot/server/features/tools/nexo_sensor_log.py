"""Receive-only, best-effort index into the unchanged full NEXO logs."""
from __future__ import annotations

import json


SENSOR_IDS = {0x58B: "LCA11", 0x4F4: "SPAS12", 0x436: "PAS11"}
MAX_BYTES = 128 * 1024 * 1024
PERIOD_NS = 5_000_000_000
STATE_FIELDS = ("leftBlindspot", "rightBlindspot", "leftBlinker", "rightBlinker",
                "gearShifter", "vEgo", "standstill")
NOTES = (
  "LCA11 interpretation: NEXO hyundai_kia_generic.dbc, CF_Lca_IndLeft=8|2@1+, "
  "CF_Lca_IndRight=16|2@1+; carstate.py uses != 0. Raw interpretation is not UI diagnosis. "
  "SPAS12/PAS11 are parking candidates only: zero, fixed or missing values do not prove sensors are off. "
  "Periodic snapshots retain original receiveMonoNs/logMonoTime; old or missing data is not a fresh observation. "
  "128 MiB (134217728 bytes) applies only to sensor_events.jsonl, independently of any full-log limit; "
  "existing full logs are unchanged and have no new size limit."
)


class SensorLog:
  def __init__(self, path, start_ns, max_bytes=MAX_BYTES):
    self.max_bytes = max_bytes
    self.bytes_written = 0
    self.records = 0
    self.limit_reached = False
    self.error = None
    self.file = None
    self.last_period_ns = start_ns
    self.car_state = None
    self.can = {}
    try:
      self.file = open(path, "wb")
    except Exception as e:
      self._fail(e)

  def _fail(self, error):
    if self.error is None:
      self.error = f"{type(error).__name__}: {error}"

  def status(self):
    return {"file": "sensor_events.jsonl", "max_bytes": self.max_bytes,
            "bytes_written": self.bytes_written, "records": self.records,
            "limit_reached": self.limit_reached, "error": self.error, "notes": NOTES}

  def _write(self, record):
    if self.error or self.limit_reached or self.file is None:
      return
    data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if self.bytes_written + len(data) > self.max_bytes:
      self.limit_reached = True
      return
    written = self.file.write(data)
    if written != len(data):
      raise OSError("short sensor log write")
    self.bytes_written += written
    self.records += 1
    if self.bytes_written == self.max_bytes:
      self.limit_reached = True

  def observe(self, service, payload, recv_ns, log_mono_time, valid=True):
    if self.error or self.limit_reached:
      return
    try:
      if service == "carState":
        values = {key: payload.get(key) for key in STATE_FIELDS}
        current = {"receiveMonoNs": recv_ns, "logMonoTime": log_mono_time,
                   "valid": valid, "data": values}
        changed = self.car_state is None or values != self.car_state["data"] or valid != self.car_state["valid"]
        self.car_state = current
        if changed:
          self._write({"kind": "carState_change", **current})
      elif service == "can":
        for frame in payload:
          address, bus = int(frame.address), int(frame.src)
          if address not in SENSOR_IDS:
            continue
          dat = bytes(frame.dat)
          key = (bus, address)
          current = {"receiveMonoNs": recv_ns, "logMonoTime": log_mono_time,
                     "valid": valid, "bus": bus, "address": address,
                     "addressHex": f"0x{address:X}", "name": SENSOR_IDS[address], "dataHex": dat.hex().upper()}
          if address == 0x58B:
            # Verified against opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc.
            current["dbc"] = "hyundai_kia_generic"
            current["CF_Lca_IndLeft"] = dat[1] & 3 if len(dat) >= 8 else None
            current["CF_Lca_IndRight"] = dat[2] & 3 if len(dat) >= 8 else None
          previous = self.can.get(key)
          self.can[key] = current
          if previous is None or previous["dataHex"] != current["dataHex"] or previous["valid"] != valid:
            self._write({"kind": "can_change", **current})
    except Exception as e:
      self._fail(e)

  def tick(self, now_ns):
    try:
      if now_ns - self.last_period_ns >= PERIOD_NS:
        self._write({"kind": "periodic", "receiveMonoNs": now_ns,
                     "carState": self.car_state, "can": [self.can[key] for key in sorted(self.can)]})
        self.last_period_ns = now_ns
    except Exception as e:
      self._fail(e)

  def flush(self):
    try:
      if self.file is not None:
        self.file.flush()
    except Exception as e:
      self._fail(e)

  def close(self):
    self.flush()
    try:
      if self.file is not None:
        self.file.close()
    except Exception as e:
      self._fail(e)
