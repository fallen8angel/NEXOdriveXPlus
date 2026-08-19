#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import subprocess
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
SAMPLE_PERIOD = 0.05
SCC12 = 0x421
CLU11 = 0x4F1
GIT_KEY_PATHS = (
  "openpilot/selfdrive/controls/controlsd.py",
  "opendbc_repo/opendbc/car/hyundai/carstate.py",
  "openpilot/selfdrive/carrot/server/features/tools",
)
SCC12_FIELDS = (
  "ACCMode",
  "StopReq",
  "aReqRaw",
  "aReqValue",
  "ACCFailInfo",
  "CF_VSM_ConfMode",
  "AEB_Status",
  "CR_VSM_Alive",
  "CR_VSM_ChkSum",
)
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


def checksum_nibble_ok(dat_b: bytes) -> bool:
  if not dat_b:
    return False
  return sum((byte >> 4) + (byte & 0xF) for byte in dat_b) % 0x10 == 0


def resolve_dbc(cp):
  if cp is None:
    return "", ""
  fingerprint = str(safe(cp, "carFingerprint", "") or "")
  if not fingerprint:
    return "", ""
  try:
    return DBC[fingerprint][Bus.pt], fingerprint
  except Exception:
    return "", fingerprint


def decode_frame(dbc_name, message_name, src, mono, addr, dat_b, parsers):
  if not dbc_name:
    return {}
  key = (message_name, src)
  try:
    parser = parsers.get(key)
    if parser is None:
      parser = CANParser(dbc_name, [(message_name, 50)], src)
      parsers[key] = parser
    parser.update([[mono, [(addr, dat_b, src)]]])
    return dict(parser.vl[message_name])
  except Exception:
    return {}


def fnum(value):
  try:
    return f"{float(value):+.3f}"
  except Exception:
    return "-"


def inum(value):
  try:
    return str(int(value))
  except Exception:
    return "-"


def scc12_text(row):
  vals = row["vals"]
  return (
    f"{row['t']:5.2f}s {row['channel']} src={row['src']} data={row['data']} "
    f"checksumNibbleOK={b(row['checksum_ok'])} | "
    f"ACCMode={inum(vals.get('ACCMode'))} StopReq={inum(vals.get('StopReq'))} "
    f"aReqRaw={fnum(vals.get('aReqRaw'))} aReqValue={fnum(vals.get('aReqValue'))} "
    f"ACCFailInfo={inum(vals.get('ACCFailInfo'))} ConfMode={inum(vals.get('CF_VSM_ConfMode'))} "
    f"AEB={inum(vals.get('AEB_Status'))} Alive={inum(vals.get('CR_VSM_Alive'))} "
    f"Chk={inum(vals.get('CR_VSM_ChkSum'))}"
  )


def state_snapshot(last):
  cs = last.get("carState")
  cc = last.get("carControl")
  co = last.get("carOutput")
  ct = last.get("controlsState")
  lp = last.get("longitudinalPlan")
  ps = last.get("pandaStates")

  cruise = safe(cs, "cruiseState", None) if cs is not None else None
  actuators = safe(cc, "actuators", None) if cc is not None else None
  applied = safe(co, "actuatorsOutput", None) if co is not None else None
  cruise_control = safe(cc, "cruiseControl", None) if cc is not None else None
  hud = safe(cc, "hudControl", None) if cc is not None else None

  controls_allowed = False
  safety_blocked = 0
  if ps is not None:
    try:
      if len(ps):
        controls_allowed = bool(safe(ps[0], "controlsAllowed", False))
        safety_blocked = int(safe(ps[0], "safetyTxBlocked", 0) or 0)
    except Exception:
      pass

  plan_accel0 = None
  plan_speed0 = None
  plan_jerk0 = None
  try:
    accels = list(safe(lp, "accels", []) or [])
    speeds = list(safe(lp, "speeds", []) or [])
    jerks = list(safe(lp, "jerks", []) or [])
    if accels:
      plan_accel0 = float(accels[0])
    if speeds:
      plan_speed0 = float(speeds[0])
    if jerks:
      plan_jerk0 = float(jerks[0])
  except Exception:
    pass

  return {
    "vEgo": float(safe(cs, "vEgo", 0.0) or 0.0) * 3.6,
    "cruiseEnabled": bool(safe(cruise, "enabled", False)),
    "cruiseAvailable": bool(safe(cruise, "available", False)),
    "vCruise": float(safe(cruise, "speed", 0.0) or 0.0) * 3.6,
    "brake": bool(safe(cs, "brakePressed", False)),
    "gas": bool(safe(cs, "gasPressed", False)),
    "standstill": bool(safe(cs, "standstill", False)),
    "ccEnabled": bool(safe(cc, "enabled", False)),
    "latActive": bool(safe(cc, "latActive", False)),
    "longActive": bool(safe(cc, "longActive", False)),
    "reqAccel": float(safe(actuators, "accel", 0.0) or 0.0),
    "appliedAccel": float(safe(applied, "accel", 0.0) or 0.0),
    "cancel": bool(safe(cruise_control, "cancel", False)),
    "resume": bool(safe(cruise_control, "resume", False)),
    "override": bool(safe(cruise_control, "override", False)),
    "activeCarrot": int(safe(hud, "activeCarrot", -1) or -1),
    "leadVisible": bool(safe(hud, "leadVisible", False)),
    "leadDistance": float(safe(hud, "leadDistance", 0.0) or 0.0),
    "longState": enum_name(safe(ct, "longControlState", "unknown")),
    "controlsAllowed": controls_allowed,
    "safetyBlocked": safety_blocked,
    "planATarget": float(safe(lp, "aTarget", 0.0) or 0.0),
    "planAccel0": plan_accel0,
    "planSpeed0": plan_speed0,
    "planJerk0": plan_jerk0,
    "planShouldStop": bool(safe(lp, "shouldStop", False)),
    "planAllowThrottle": bool(safe(lp, "allowThrottle", False)),
    "planHasLead": bool(safe(lp, "hasLead", False)),
    "planSource": enum_name(safe(lp, "longitudinalPlanSource", "unknown")),
  }


def plan_chain_text(t, s, scc):
  scc_vals = scc["vals"] if scc is not None else {}
  scc_age = "-" if scc is None else f"{t - scc['t']:+.3f}s"
  return (
    f"{t:5.2f}s mode={s['activeCarrot']} cc.enabled={b(s['ccEnabled'])} lat={b(s['latActive'])} long={b(s['longActive'])} "
    f"cruise={b(s['cruiseEnabled'])} vEgo={s['vEgo']:.1f} target={s['vCruise']:.1f} "
    f"brake={b(s['brake'])} gas={b(s['gas'])} cancel={b(s['cancel'])} resume={b(s['resume'])} override={b(s['override'])} | "
    f"plan source={s['planSource']} hasLead={b(s['planHasLead'])} shouldStop={b(s['planShouldStop'])} "
    f"allowThrottle={b(s['planAllowThrottle'])} aTarget={s['planATarget']:+.3f} "
    f"accel0={'-' if s['planAccel0'] is None else f'{s['planAccel0']:+.3f}'} "
    f"jerk0={'-' if s['planJerk0'] is None else f'{s['planJerk0']:+.3f}'} | "
    f"carControl.req={s['reqAccel']:+.3f} applied={s['appliedAccel']:+.3f} longState={s['longState']} | "
    f"SCC12(age={scc_age}) ACCMode={inum(scc_vals.get('ACCMode'))} StopReq={inum(scc_vals.get('StopReq'))} "
    f"aReqRaw={fnum(scc_vals.get('aReqRaw'))} aReqValue={fnum(scc_vals.get('aReqValue'))} "
    f"controlsAllowed={b(s['controlsAllowed'])} blocked={s['safetyBlocked']}"
  )


def run_git(args, timeout=1.5):
  try:
    proc = subprocess.run(
      ["git", *args], cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
      text=True, timeout=timeout, check=False,
    )
    return proc.stdout.rstrip()
  except Exception as e:
    return f"<git error: {type(e).__name__}: {e}>"


def git_snapshot():
  head = run_git(["rev-parse", "HEAD"])
  branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])

  # Keep the global check cheap and bounded. The old full status/diff could
  # walk large generated/untracked trees and time out on the device.
  status = run_git([
    "status", "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=all",
  ], timeout=1.0)
  key_status = run_git([
    "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all", "--", *GIT_KEY_PATHS,
  ], timeout=1.0)
  diff = run_git([
    "diff", "HEAD", "--no-ext-diff", "--ignore-submodules=all", "--unified=0", "--", *GIT_KEY_PATHS,
  ], timeout=2.0)
  diff_hash = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()

  untracked = []
  for line in key_status.splitlines():
    if not line.startswith("?? "):
      continue
    path = line[3:]
    full = os.path.join(REPO_ROOT, path)
    try:
      if os.path.isfile(full):
        h = hashlib.sha256()
        size = 0
        with open(full, "rb") as f:
          while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
              break
            size += len(chunk)
            h.update(chunk)
        untracked.append(f"{path} size={size} sha256={h.hexdigest()}")
    except Exception as e:
      untracked.append(f"{path} hash_error={type(e).__name__}: {e}")
    if len(untracked) >= 50:
      break

  preview_limit = 65536
  preview = diff[:preview_limit]
  truncated = len(diff) > preview_limit
  return head, branch, status, key_status, diff_hash, preview, truncated, untracked


def main() -> int:
  services = [
    "carState", "carControl", "carOutput", "controlsState", "longitudinalPlan",
    "pandaStates", "carParams", "can", "sendcan",
  ]
  sm = messaging.SubMaster(services)
  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS
  last = {}
  service_updates = Counter()
  parsers = {}
  dbc_name = ""
  fingerprint = ""

  scc_rows = []
  scc_send_latest = None
  block_events = []
  last_blocked = None
  chain_rows = []
  positive_rows = []
  transition_rows = []
  next_sample = 0.0
  prev_transition_key = None

  button_rows = []
  button_counts = Counter()
  button_parsers = {}
  prev_button = {}

  while time.monotonic() < deadline:
    sm.update(25)
    now = time.monotonic() - start

    for service in services:
      try:
        if sm.updated[service]:
          service_updates[service] += 1
          last[service] = sm[service]
      except Exception:
        pass

    if not dbc_name and last.get("carParams") is not None:
      dbc_name, fingerprint = resolve_dbc(last.get("carParams"))

    try:
      if sm.updated["sendcan"]:
        mono = int(sm.logMonoTime["sendcan"])
        for frame in list(sm["sendcan"]):
          addr = int(safe(frame, "address", -1))
          if addr != SCC12:
            continue
          src = int(safe(frame, "src", -1))
          dat_b = bytes(safe(frame, "dat", b""))
          vals = decode_frame(dbc_name, "SCC12", src, mono, addr, dat_b, parsers)
          row = {
            "t": now, "channel": "sendcan", "src": src, "data": dat_b.hex().upper(),
            "checksum_ok": checksum_nibble_ok(dat_b), "vals": vals,
          }
          scc_rows.append(row)
          scc_send_latest = row
    except Exception:
      pass

    try:
      if sm.updated["can"]:
        mono = int(sm.logMonoTime["can"])
        clu_grouped = defaultdict(list)
        for frame in list(sm["can"]):
          src = int(safe(frame, "src", -1))
          addr = int(safe(frame, "address", -1))
          dat_b = bytes(safe(frame, "dat", b""))
          dat = dat_b.hex().upper()

          if addr == SCC12:
            vals = decode_frame(dbc_name, "SCC12", src, mono, addr, dat_b, parsers)
            scc_rows.append({
              "t": now, "channel": "can", "src": src, "data": dat,
              "checksum_ok": checksum_nibble_ok(dat_b), "vals": vals,
            })

          if addr == CLU11:
            clu_grouped[src].append((addr, dat_b, src))
            button_counts[src] += 1

        if dbc_name:
          for src, frames in clu_grouped.items():
            try:
              parser = button_parsers.get(src)
              if parser is None:
                parser = CANParser(dbc_name, [("CLU11", 50)], src)
                button_parsers[src] = parser
              parser.update([[mono, frames]])
              vals = parser.vl["CLU11"]
              sw = int(vals.get("CF_Clu_CruiseSwState", -1))
              main_sw = int(vals.get("CF_Clu_CruiseSwMain", -1))
              raw = frames[-1][1].hex().upper()
              state = (sw, main_sw, raw)
              if prev_button.get(src) != state and len(button_rows) < 160:
                button_rows.append(
                  f"{now:5.2f}s src={src} raw={raw} CruiseSwState={sw}({BUTTON_NAMES.get(sw, f'UNKNOWN({sw})')}) "
                  f"CruiseSwMain={main_sw}({'MODE/MAIN' if main_sw > 0 else 'OFF'})"
                )
                prev_button[src] = state
            except Exception as e:
              if len(button_rows) < 160:
                button_rows.append(f"{now:5.2f}s src={src} decode_error={type(e).__name__}: {e}")
    except Exception:
      pass

    try:
      if sm.updated["pandaStates"]:
        ps = sm["pandaStates"]
        if len(ps):
          blocked = int(safe(ps[0], "safetyTxBlocked", 0) or 0)
          if last_blocked is None:
            last_blocked = blocked
          elif blocked != last_blocked:
            block_events.append((now, last_blocked, blocked, state_snapshot(last)))
            last_blocked = blocked
    except Exception:
      pass

    if now >= next_sample:
      snap = state_snapshot(last)
      if len(chain_rows) < 180:
        chain_rows.append((now, snap, scc_send_latest))
      if snap["longActive"] and (snap["reqAccel"] > 0.05 or snap["appliedAccel"] > 0.05) and len(positive_rows) < 80:
        positive_rows.append((now, snap, scc_send_latest))

      transition_key = (
        snap["ccEnabled"], snap["latActive"], snap["longActive"], snap["cruiseEnabled"],
        snap["controlsAllowed"], snap["brake"], snap["gas"], snap["cancel"], snap["resume"], snap["activeCarrot"],
      )
      if transition_key != prev_transition_key and len(transition_rows) < 120:
        transition_rows.append((now, snap, scc_send_latest))
        prev_transition_key = transition_key
      next_sample += SAMPLE_PERIOD

  print("")
  print("[23] SCC12 0x421 완전 해독 · TX/echo/reject")
  print(f"  fingerprint={fingerprint or '-'} dbc={dbc_name or '-'}")
  if scc_rows:
    interesting = []
    last_key = None
    for row in scc_rows:
      vals = row["vals"]
      key = (
        row["channel"], row["src"], vals.get("ACCMode"), vals.get("StopReq"),
        round(float(vals.get("aReqRaw", 0.0) or 0.0), 3),
        round(float(vals.get("aReqValue", 0.0) or 0.0), 3),
        vals.get("CF_VSM_ConfMode"), vals.get("AEB_Status"), row["checksum_ok"],
      )
      if key != last_key or row["src"] >= 192:
        interesting.append(row)
        last_key = key
      if len(interesting) >= 180:
        break
    for row in interesting:
      print("  " + scc12_text(row))
  else:
    print("  SCC12 관측 없음")

  print("")
  print("[24] safetyTxBlocked 발생 전후 SCC12 ±0.30초")
  if block_events:
    for idx, (t, old, new, snap) in enumerate(block_events, 1):
      print(
        f"  EVENT#{idx} {t:5.2f}s safetyTxBlocked {old}->{new} delta={new-old:+d} | "
        f"longActive={b(snap['longActive'])} controlsAllowed={b(snap['controlsAllowed'])} "
        f"brake={b(snap['brake'])} gas={b(snap['gas'])} reqAccel={snap['reqAccel']:+.3f} applied={snap['appliedAccel']:+.3f}"
      )
      window = [row for row in scc_rows if abs(row["t"] - t) <= 0.30]
      if window:
        for row in window[-40:]:
          print("    " + scc12_text(row))
      else:
        print("    근접 SCC12 프레임 없음")
  else:
    print("  8초 동안 safetyTxBlocked 증가 없음")
  print("  ※ source>=192와 block counter의 시간 상관관계를 보여주며 Panda 내부 reject 사유를 임의로 단정하지 않습니다.")

  print("")
  print("[25] longitudinalPlan → carControl → 실제 SCC12 명령 경로")
  print("  [MED/SPEED/controlsAllowed/페달 상태 변화]")
  for t, snap, scc in transition_rows:
    print("   " + plan_chain_text(t, snap, scc))
  print("  [양(+)의 가속 명령 샘플]")
  if positive_rows:
    for t, snap, scc in positive_rows:
      print("   " + plan_chain_text(t, snap, scc))
  else:
    print("   longActive 상태에서 +0.05m/s² 초과 양의 가속 명령 관측 없음")
  print("  [50ms 샘플 전체 - 최대 180행]")
  for t, snap, scc in chain_rows:
    print("   " + plan_chain_text(t, snap, scc))

  print("")
  print("[26] CLU11 버튼 RAW · DBC 해독 + 실행 Git 상태")
  if button_counts:
    print("  CLU11 RX: " + " | ".join(f"src {src}={count}" for src, count in sorted(button_counts.items())))
  if button_rows:
    for row in button_rows:
      print("  " + row)
  else:
    print("  CLU11 버튼 변화 관측 없음")

  head, branch, status, key_status, diff_hash, diff_preview, diff_truncated, untracked = git_snapshot()
  print("  [runtime git]")
  print(f"   branch={branch or '-'}")
  print(f"   HEAD={head or '-'}")
  dirty = bool(status.strip() or key_status.strip())
  print(f"   dirty={b(dirty)}")
  print(f"   gitDiffSha256={diff_hash} scope=key_paths")
  if status.strip():
    print("   tracked status --porcelain (-uno, ignore-submodules):")
    for line in status.splitlines()[:120]:
      print("    " + line)
  else:
    print("   tracked status: clean")
  if key_status.strip():
    print("   key-path status --porcelain:")
    for line in key_status.splitlines()[:120]:
      print("    " + line)
  else:
    print("   key-path status: clean")
  if untracked:
    print("   key-path untracked sha256:")
    for line in untracked:
      print("    " + line)
  if diff_preview.strip():
    print("   git diff HEAD --unified=0 key-path preview:")
    for line in diff_preview.splitlines():
      print("    " + line)
    if diff_truncated:
      print("    ... DIFF_PREVIEW_TRUNCATED_AT_65536_CHARS ...")
  else:
    print("   key-path git diff HEAD: empty")

  print("")
  print("  [forensic service updates]")
  for service in services:
    print(f"   {service}: updates={service_updates[service]}")

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
