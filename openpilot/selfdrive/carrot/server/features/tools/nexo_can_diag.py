#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

REPO_ROOT = "/data/openpilot"
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from openpilot.cereal import messaging
from openpilot.common.params import Params

OBSERVE_SECONDS = 8.0
HYUNDAI_SCC_IDS = {0x420: "SCC11", 0x421: "SCC12", 0x50A: "SCC13", 0x389: "SCC14", 0x38D: "FCA11"}
WATCH_PROCS = {"card", "selfdrived", "controlsd", "radard", "radard_dpath", "pandad", "ui"}


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def enum_name(value) -> str:
  try:
    return str(value).split(".")[-1]
  except Exception:
    return str(value)


def valid(sm, service: str) -> bool:
  try:
    return bool(sm.valid.get(service, False))
  except Exception:
    return False


def updated(sm, service: str) -> bool:
  try:
    return bool(sm.updated.get(service, False))
  except Exception:
    return False


def b(v) -> str:
  return "True" if bool(v) else "False"


def main() -> int:
  services = ["pandaStates", "deviceState", "managerState", "carParams", "carState", "selfdriveState", "controlsState", "radarState", "can"]
  sm = messaging.SubMaster(services)
  params = Params()

  start_wall = datetime.now()
  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS
  counts = Counter()
  bus_counts = Counter()
  addr_counts = defaultdict(Counter)
  scc_counts = defaultdict(Counter)
  observed_faults = set()
  last = {}

  while time.monotonic() < deadline:
    sm.update(100)
    for s in services:
      if updated(sm, s):
        counts[s] += 1
        last[s] = sm[s]

    ps = last.get("pandaStates")
    if ps is not None and len(ps):
      for fault in list(safe(ps[0], "faults", [])):
        observed_faults.add(enum_name(fault))

    if updated(sm, "can"):
      try:
        frames = list(sm["can"])
      except Exception:
        frames = []
      for f in frames:
        bus = int(safe(f, "src", -1))
        addr = int(safe(f, "address", -1))
        bus_counts[bus] += 1
        addr_counts[bus][addr] += 1
        if addr in HYUNDAI_SCC_IDS:
          scc_counts[bus][addr] += 1

  elapsed = max(0.001, time.monotonic() - start)
  panda = None
  if last.get("pandaStates") is not None and len(last["pandaStates"]):
    panda = last["pandaStates"][0]

  current_faults = [] if panda is None else [enum_name(x) for x in list(safe(panda, "faults", []))]
  all_faults = sorted(set(current_faults) | observed_faults)
  can_seen = sum(bus_counts.values()) > 0
  device = last.get("deviceState")
  started = bool(safe(device, "started", False)) if device is not None else False
  car_valid = valid(sm, "carState") and last.get("carState") is not None

  if all_faults or not can_seen:
    verdict = "[주행 금지]"
  elif not started or not car_valid:
    verdict = "[주의]"
  else:
    verdict = "[정상 후보]"

  lines = []
  add = lines.append
  add("=" * 68)
  add("NEXOdriveXPlus 8초 통합진단")
  add("=" * 68)
  add(f"실행시각: {start_wall.strftime('%Y-%m-%d %H:%M:%S')}")
  add(f"관측시간: {elapsed:.2f}초")
  add(f"판정: {verdict}")
  add("")

  add("[1] Panda fault · 안전상태")
  if panda is None:
    add("Panda: 수신 없음")
  else:
    add(f"현재 fault: {', '.join(current_faults) if current_faults else '없음'}")
    add(f"8초 관측 fault: {', '.join(all_faults) if all_faults else '없음'}")
    add(f"safety={enum_name(safe(panda, 'safetyModel', 'unknown'))}({safe(panda, 'safetyParam', '-')}) | controlsAllowed={b(safe(panda, 'controlsAllowed', False))} | rxChecksInvalid={b(safe(panda, 'rxChecksInvalid', False))} | faultStatus={enum_name(safe(panda, 'faultStatus', 'unknown'))}")
    add(f"ignitionLine={b(safe(panda, 'ignitionLine', False))} | ignitionCan={b(safe(panda, 'ignitionCan', False))} | interruptLoad={safe(panda, 'interruptLoad', '-')}")
  add("")

  add("[2] 시작 조건 · manager")
  if device is None:
    add("deviceState: 수신 없음")
  else:
    add(f"deviceState valid={b(valid(sm, 'deviceState'))} | started={b(started)}")
  add(f"ControlsReady={b(params.get_bool('ControlsReady'))} | FirmwareQueryDone={b(params.get_bool('FirmwareQueryDone'))} | OpenpilotEnabledToggle={b(params.get_bool('OpenpilotEnabledToggle'))}")
  add(f"CarSelected3={params.get('CarSelected3', encoding='utf-8') or '-'}")
  add(f"CarName={params.get('CarName', encoding='utf-8') or '-'}")

  ms = last.get("managerState")
  if ms is None:
    add("managerState: 수신 없음")
  else:
    found = []
    for p in list(safe(ms, "processes", [])):
      name = str(safe(p, "name", ""))
      if name in WATCH_PROCS:
        found.append(f"{name}: running={b(safe(p, 'running', False))} shouldBeRunning={b(safe(p, 'shouldBeRunning', False))} exitCode={safe(p, 'exitCode', '-')}")
    add("manager 프로세스:")
    for row in found:
      add("  " + row)
  add("")

  add("[3] raw CAN 8초 수신량")
  for bus in sorted(bus_counts):
    add(f"source {bus}: {bus_counts[bus]} frames | {bus_counts[bus] / elapsed:.1f} frames/sec")
    top = addr_counts[bus].most_common(8)
    if top:
      add("  상위 ID: " + ", ".join(f"0x{a:X}={c}" for a, c in top))
  if not bus_counts:
    add("CAN 프레임 수신 없음")
  add("")

  add("[4] 현대 SCC/FCA 메시지 관측")
  any_scc = False
  for bus in sorted(scc_counts):
    entries = []
    for addr, cnt in sorted(scc_counts[bus].items()):
      any_scc = True
      entries.append(f"{HYUNDAI_SCC_IDS[addr]}=0x{addr:X}:{cnt}")
    if entries:
      add(f"source {bus}: " + " | ".join(entries))
  if not any_scc:
    add("SCC/FCA 관측 없음")
  add("")

  add("[5] carParams · carState")
  cp = last.get("carParams")
  if cp is None:
    add("carParams: 수신 없음")
  else:
    add(f"carParams valid={b(valid(sm, 'carParams'))} | fingerprint={safe(cp, 'carFingerprint', '-')} | brand={safe(cp, 'brand', '-')} | openpilotLong={b(safe(cp, 'openpilotLongitudinalControl', False))} | pcmCruise={b(safe(cp, 'pcmCruise', False))}")
  cs = last.get("carState")
  if cs is None:
    add("carState: 수신 없음")
  else:
    add(f"carState valid={b(valid(sm, 'carState'))} | gear={enum_name(safe(cs, 'gearShifter', 'unknown'))} | speed={float(safe(cs, 'vEgo', 0.0))*3.6:.1f} km/h")
  add("")

  add("[6] selfdrive · controls · radar")
  for service in ("selfdriveState", "controlsState", "radarState"):
    add(f"{service}: updates={counts[service]} | valid={b(valid(sm, service))}")
  add("")

  add("[7] 전체 서비스 수신 현황")
  for s in services:
    add(f"{s}: updates={counts[s]} | valid={b(valid(sm, s))}")
  add("")

  add("[8] 핵심 판정")
  if all_faults:
    add("[주행 금지] Panda fault 감지: " + ", ".join(all_faults))
  elif not can_seen:
    add("[주행 금지] raw CAN 수신 없음")
  elif not started:
    add("[주의] deviceState.started=False: manager가 card/selfdrived/controlsd/radard를 시작하지 않는 상태입니다.")
  elif counts["carState"] == 0:
    add("[주의] started=True이지만 carState=0: card 프로세스 시작/초기화 오류를 확인해야 합니다.")
  elif not car_valid:
    add("[주의] carState는 수신되지만 valid=False입니다.")
  else:
    add("[정상 후보] onroad 시작과 carState 수신이 확인됩니다.")

  add("")
  add("※ 이 진단은 상태 확인용이며 안전 제한이나 Panda fault를 우회하지 않습니다.")
  print("\n".join(lines))
  return 0 if verdict == "[정상 후보]" else 1


if __name__ == "__main__":
  raise SystemExit(main())
