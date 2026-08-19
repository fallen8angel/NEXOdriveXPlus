#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict

REPO_ROOT = "/data/openpilot"
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from openpilot.cereal import messaging
from opendbc.can import CANParser
from opendbc.car import Bus
from opendbc.car.hyundai.values import DBC

OBSERVE_SECONDS = 8.0
STATE_SAMPLE_PERIOD = 0.10
CLU11 = 0x4F1
SCC12 = 0x421
RADAR_IDS = set(range(0x500, 0x520))
BUTTON_NAMES = {
  0: "NONE",
  1: "RES+",
  2: "SET-",
  3: "GAP",
  4: "CANCEL",
}


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def b(value):
  return "True" if bool(value) else "False"


def enum_name(value):
  try:
    return str(value).split(".")[-1]
  except Exception:
    return str(value)


def payload_hex(frame):
  try:
    return bytes(safe(frame, "dat", b"")).hex().upper()
  except Exception:
    return str(safe(frame, "dat", ""))


def resolve_clu11(cp):
  if cp is None:
    return "", None, ""
  fingerprint = str(safe(cp, "carFingerprint", "") or "")
  if not fingerprint:
    return "", None, ""
  try:
    dbc_name = DBC[fingerprint][Bus.pt]
    probe = CANParser(dbc_name, [], 0)
    msg = probe.dbc.name_to_msg.get("CLU11")
    return dbc_name, (None if msg is None else int(msg.address)), fingerprint
  except Exception as e:
    return "", None, f"{fingerprint} ({type(e).__name__}: {e})"


def state_snapshot(last):
  cs = last.get("carState")
  cc = last.get("carControl")
  co = last.get("carOutput")
  ct = last.get("controlsState")
  ps = last.get("pandaStates")

  cruise = safe(cs, "cruiseState", None) if cs is not None else None
  actuators = safe(cc, "actuators", None) if cc is not None else None
  applied = safe(co, "actuatorsOutput", None) if co is not None else None

  controls_allowed = False
  safety_blocked = 0
  if ps is not None:
    try:
      if len(ps):
        controls_allowed = bool(safe(ps[0], "controlsAllowed", False))
        safety_blocked = int(safe(ps[0], "safetyTxBlocked", 0) or 0)
    except Exception:
      pass

  return {
    "vEgo": float(safe(cs, "vEgo", 0.0) or 0.0) * 3.6,
    "available": bool(safe(cruise, "available", False)),
    "enabled": bool(safe(cruise, "enabled", False)),
    "vCruise": float(safe(cruise, "speed", 0.0) or 0.0) * 3.6,
    "standstill": bool(safe(cs, "standstill", False)),
    "brake": bool(safe(cs, "brakePressed", False)),
    "gas": bool(safe(cs, "gasPressed", False)),
    "latActive": bool(safe(cc, "latActive", False)),
    "longActive": bool(safe(cc, "longActive", False)),
    "accel": float(safe(actuators, "accel", 0.0) or 0.0),
    "appliedAccel": float(safe(applied, "accel", 0.0) or 0.0),
    "longState": enum_name(safe(ct, "longControlState", "unknown")),
    "controlsAllowed": controls_allowed,
    "safetyBlocked": safety_blocked,
  }


def state_text(s):
  return (
    f"vEgo={s['vEgo']:.1f}km/h cruise.available={b(s['available'])} cruise.enabled={b(s['enabled'])} "
    f"target={s['vCruise']:.1f}km/h standstill={b(s['standstill'])} brake={b(s['brake'])} gas={b(s['gas'])} | "
    f"latActive={b(s['latActive'])} longActive={b(s['longActive'])} accel={s['accel']:+.3f} "
    f"appliedAccel={s['appliedAccel']:+.3f} longState={s['longState']} | "
    f"controlsAllowed={b(s['controlsAllowed'])} safetyTxBlocked={s['safetyBlocked']}"
  )


def lead_snapshot(rs):
  lead = safe(rs, "leadOne", None) if rs is not None else None
  return (
    bool(safe(lead, "status", False)),
    float(safe(lead, "dRel", 0.0) or 0.0),
    float(safe(lead, "vRel", 0.0) or 0.0),
    float(safe(lead, "aRel", 0.0) or 0.0),
    float(safe(lead, "vLead", 0.0) or 0.0),
  )


def main() -> int:
  services = ["carState", "carControl", "carOutput", "controlsState", "radarState", "pandaStates", "carParams", "can", "sendcan"]
  sm = messaging.SubMaster(services)

  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS
  last = {}
  service_updates = Counter()

  state_rows = []
  last_state_key = None
  next_sample = 0.0

  block_rows = []
  last_blocked = None
  reject_candidates = []
  latest_reject = None
  scc12_send_latest = None

  button_rows = []
  button_counts = Counter()
  prev_button_state = {}
  parsers = {}
  dbc_name = ""
  clu_addr = None
  fingerprint = ""

  lead_rows = []
  prev_lead = None
  radar_counts = Counter()
  radar_first = {}
  radar_last = {}
  radar_payload_first = {}
  radar_payload_last = {}

  while time.monotonic() < deadline:
    sm.update(50)
    now = time.monotonic() - start

    for service in services:
      try:
        if sm.updated[service]:
          service_updates[service] += 1
          last[service] = sm[service]
      except Exception:
        pass

    if (not dbc_name or clu_addr is None) and last.get("carParams") is not None:
      dbc_name, clu_addr, fingerprint = resolve_clu11(last.get("carParams"))

    if now >= next_sample:
      snap = state_snapshot(last)
      key = tuple(snap.items())
      if key != last_state_key and len(state_rows) < 120:
        state_rows.append(f"{now:5.2f}s {state_text(snap)}")
        last_state_key = key
      next_sample += STATE_SAMPLE_PERIOD

    try:
      if sm.updated["pandaStates"]:
        ps = sm["pandaStates"]
        if len(ps):
          blocked = int(safe(ps[0], "safetyTxBlocked", 0) or 0)
          if last_blocked is None:
            last_blocked = blocked
          elif blocked != last_blocked:
            delta = blocked - last_blocked
            snap = state_snapshot(last)
            candidate = "none"
            if latest_reject is not None and now - latest_reject[0] <= 0.10:
              _, src, addr, dat = latest_reject
              candidate = f"src={src} id=0x{addr:X} data={dat}"
            if len(block_rows) < 100:
              block_rows.append(
                f"{now:5.2f}s safetyTxBlocked {last_blocked}->{blocked} delta={delta:+d} | {candidate} | "
                f"longActive={b(snap['longActive'])} brake={b(snap['brake'])} gas={b(snap['gas'])} "
                f"controlsAllowed={b(snap['controlsAllowed'])} | reason=not_exposed_by_pandaStates"
              )
            last_blocked = blocked
    except Exception:
      pass

    try:
      if sm.updated["radarState"]:
        lead = lead_snapshot(sm["radarState"])
        rounded = (lead[0], round(lead[1], 1), round(lead[2], 1), round(lead[3], 1), round(lead[4], 1))
        if rounded != prev_lead and len(lead_rows) < 100:
          lead_rows.append(
            f"{now:5.2f}s leadOne status={b(lead[0])} dRel={lead[1]:.1f}m vRel={lead[2]:+.2f}m/s "
            f"aRel={lead[3]:+.2f}m/s² vLead={lead[4]:.2f}m/s"
          )
          prev_lead = rounded
    except Exception:
      pass

    try:
      if sm.updated["sendcan"]:
        for frame in list(sm["sendcan"]):
          addr = int(safe(frame, "address", -1))
          if addr == SCC12:
            scc12_send_latest = (now, int(safe(frame, "src", -1)), payload_hex(frame))
    except Exception:
      pass

    try:
      if sm.updated["can"]:
        grouped_clu = defaultdict(list)
        mono = int(sm.logMonoTime["can"])
        for frame in list(sm["can"]):
          src = int(safe(frame, "src", -1))
          addr = int(safe(frame, "address", -1))
          dat_b = bytes(safe(frame, "dat", b""))
          dat = dat_b.hex().upper()

          if src >= 192:
            latest_reject = (now, src, addr, dat)
            if len(reject_candidates) < 120:
              reject_candidates.append(f"{now:5.2f}s src={src} id=0x{addr:X} data={dat}")

          if addr in RADAR_IDS:
            key = (src, addr)
            radar_counts[key] += 1
            radar_first.setdefault(key, now)
            radar_last[key] = now
            radar_payload_first.setdefault(key, dat)
            radar_payload_last[key] = dat

          if clu_addr is not None and addr == clu_addr:
            grouped_clu[src].append((addr, dat_b, src))
            button_counts[src] += 1

        if dbc_name and clu_addr is not None:
          for src, frames in grouped_clu.items():
            parser = parsers.get(src)
            if parser is None:
              parser = CANParser(dbc_name, [("CLU11", 50)], src)
              parsers[src] = parser
            parser.update([[mono, frames]])
            vals = parser.vl["CLU11"]
            sw = int(vals.get("CF_Clu_CruiseSwState", -1))
            main_sw = int(vals.get("CF_Clu_CruiseSwMain", -1))
            state = (sw, main_sw)
            if prev_button_state.get(src) != state and len(button_rows) < 100:
              label = BUTTON_NAMES.get(sw, f"UNKNOWN({sw})")
              main_label = "MODE/MAIN" if main_sw > 0 else "OFF"
              button_rows.append(
                f"{now:5.2f}s CLU11 src={src} CruiseSwState={sw}({label}) CruiseSwMain={main_sw}({main_label})"
              )
              prev_button_state[src] = state
    except Exception as e:
      if len(button_rows) < 100:
        button_rows.append(f"{now:5.2f}s CLU11 decode error: {type(e).__name__}: {e}")

  print("")
  print("[18] 롱컨 실제 명령 · 페달 · 정차 상태")
  print("※ 기존 8초 진단은 유지하고 carControl/carOutput/carState/controlsState를 추가 관측합니다.")
  if state_rows:
    for row in state_rows:
      print("  " + row)
  else:
    print("  상태 샘플 관측 없음")

  print("")
  print("[19] Panda TX 차단 상세 · source192 상관관계")
  if block_rows:
    for row in block_rows:
      print("  " + row)
  else:
    print("  8초 동안 safetyTxBlocked 증가 없음")
  print(f"  source>=192 후보 프레임={len(reject_candidates)}")
  for row in reject_candidates[:60]:
    print("   " + row)
  if scc12_send_latest is not None:
    t, src, dat = scc12_send_latest
    print(f"  마지막 sendcan SCC12: {t:5.2f}s src={src} data={dat}")
  print("  ※ PandaStates는 차단 사유 문자열을 제공하지 않으므로 reason은 추정하지 않고 counter와 후보 프레임만 시간상으로 맞춰 표시합니다.")

  print("")
  print("[20] MODE · RES · SET · GAP · CANCEL DBC 해석")
  print(f"  fingerprint={fingerprint or '-'} | dbc={dbc_name or '-'} | CLU11={'-' if clu_addr is None else f'0x{clu_addr:X}'}")
  if button_counts:
    print("  CLU11 RX: " + " | ".join(f"src {src}={count}" for src, count in sorted(button_counts.items())))
  if button_rows:
    for row in button_rows:
      print("  " + row)
  else:
    print("  CLU11 버튼 변화 관측 없음")

  print("")
  print("[21] radarState leadOne · raw 0x500~0x51F")
  if lead_rows:
    for row in lead_rows:
      print("  " + row)
  else:
    print("  leadOne 변화 관측 없음")
  if radar_counts:
    print("  [raw radar track 요약]")
    for (src, addr), count in sorted(radar_counts.items()):
      print(
        f"   src={src} id=0x{addr:X} count={count} first={radar_first[(src, addr)]:.2f}s last={radar_last[(src, addr)]:.2f}s "
        f"firstData={radar_payload_first[(src, addr)]} lastData={radar_payload_last[(src, addr)]}"
      )
  else:
    print("  raw radar track 0x500~0x51F 관측 없음")

  print("")
  print("[22] 롱컨 상세 서비스 수신 현황")
  for service in services:
    print(f"  {service}: updates={service_updates[service]}")

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
