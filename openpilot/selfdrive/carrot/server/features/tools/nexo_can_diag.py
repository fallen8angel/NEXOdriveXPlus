#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

# The 7000 web server launches this file directly. Add the repository root so
# openpilot.cereal resolves even when the current working directory is not the
# repository root.
REPO_ROOT = "/data/openpilot"
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from openpilot.cereal import messaging


OBSERVE_SECONDS = 8.0
HYUNDAI_SCC_IDS = {
  0x420: "SCC11",
  0x421: "SCC12",
  0x50A: "SCC13",
  0x389: "SCC14",
  0x38D: "FCA11",
}


def enum_name(value) -> str:
  try:
    return str(value).split(".")[-1]
  except Exception:
    return str(value)


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def fmt_bool(value) -> str:
  return "True" if bool(value) else "False"


def fmt_float(value, digits=3, default="-") -> str:
  try:
    return f"{float(value):.{digits}f}"
  except Exception:
    return default


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


def main() -> int:
  services = ["pandaStates", "carState", "selfdriveState", "controlsState", "radarState", "can"]
  sm = messaging.SubMaster(services)

  start_wall = datetime.now()
  start_mono = time.monotonic()
  deadline = start_mono + OBSERVE_SECONDS

  last_panda = None
  last_car = None
  last_selfdrive = None
  last_controls = None
  last_radar = None
  observed_faults: set[str] = set()
  service_updates = Counter()
  bus_frame_counts = Counter()
  bus_address_counts: dict[int, Counter] = defaultdict(Counter)
  scc_counts: dict[int, Counter] = defaultdict(Counter)

  while time.monotonic() < deadline:
    sm.update(100)
    for service in services:
      if updated(sm, service):
        service_updates[service] += 1

    if updated(sm, "pandaStates") and len(sm["pandaStates"]):
      last_panda = sm["pandaStates"][0]
      for fault in list(safe(last_panda, "faults", [])):
        observed_faults.add(enum_name(fault))

    if updated(sm, "carState"):
      last_car = sm["carState"]
    if updated(sm, "selfdriveState"):
      last_selfdrive = sm["selfdriveState"]
    if updated(sm, "controlsState"):
      last_controls = sm["controlsState"]
    if updated(sm, "radarState"):
      last_radar = sm["radarState"]

    if updated(sm, "can"):
      try:
        frames = list(sm["can"])
      except Exception:
        frames = []
      for frame in frames:
        bus = int(safe(frame, "src", -1))
        address = int(safe(frame, "address", -1))
        bus_frame_counts[bus] += 1
        bus_address_counts[bus][address] += 1
        if address in HYUNDAI_SCC_IDS:
          scc_counts[bus][address] += 1

  elapsed = max(0.001, time.monotonic() - start_mono)

  current_faults: list[str] = []
  rx_invalid = False
  fault_status = "unknown"
  controls_allowed = False
  interrupt_load = None
  safety_model = "unknown"
  safety_param = "-"
  safety_tx_blocked = None

  if last_panda is not None:
    current_faults = [enum_name(x) for x in list(safe(last_panda, "faults", []))]
    rx_invalid = bool(safe(last_panda, "rxChecksInvalid", False))
    fault_status = enum_name(safe(last_panda, "faultStatus", "unknown"))
    controls_allowed = bool(safe(last_panda, "controlsAllowed", False))
    interrupt_load = safe(last_panda, "interruptLoad", None)
    safety_model = enum_name(safe(last_panda, "safetyModel", "unknown"))
    safety_param = safe(last_panda, "safetyParam", "-")
    safety_tx_blocked = safe(last_panda, "safetyTxBlocked", None)

  all_faults = sorted(set(current_faults) | observed_faults)
  car_valid = valid(sm, "carState") and last_car is not None
  panda_ok = last_panda is not None and not all_faults and not rx_invalid
  can_seen = sum(bus_frame_counts.values()) > 0

  if last_panda is None or all_faults or rx_invalid or not can_seen:
    verdict = "[주행 금지]"
  elif not car_valid:
    verdict = "[주의]"
  else:
    verdict = "[정상 후보]"

  lines: list[str] = []
  add = lines.append
  add("=" * 68)
  add("NEXOdriveXPlus 8초 통합진단")
  add("=" * 68)
  add(f"실행시각: {start_wall.strftime('%Y-%m-%d %H:%M:%S')}")
  add(f"관측시간: {elapsed:.2f}초")
  add(f"판정: {verdict}")
  add("")

  add("[1] Panda fault · 안전상태")
  if last_panda is None:
    add("Panda: 수신 없음")
  else:
    add(f"현재 fault: {', '.join(current_faults) if current_faults else '없음'}")
    add(f"8초 관측 fault: {', '.join(all_faults) if all_faults else '없음'}")
    add(
      f"safety={safety_model}({safety_param}) | controlsAllowed={fmt_bool(controls_allowed)} | "
      f"rxChecksInvalid={fmt_bool(rx_invalid)} | faultStatus={fault_status}"
    )
    if interrupt_load is not None:
      add(f"interruptLoad={fmt_float(interrupt_load, 4)}")
    if safety_tx_blocked is not None:
      add(f"safetyTxBlocked={safety_tx_blocked}")

    can_states = list(safe(last_panda, "canState", []))
    if not can_states:
      add("CAN core 상태: 없음")
    for i, cs in enumerate(can_states):
      add(
        f"CAN core {i}: speed={safe(cs, 'canSpeed', safe(cs, 'speed', '-'))} | "
        f"irq0/irq1={safe(cs, 'irq0CallRate', 0)}/{safe(cs, 'irq1CallRate', 0)} per sec | "
        f"busOff={fmt_bool(safe(cs, 'busOff', False))} | "
        f"warn/passive={fmt_bool(safe(cs, 'errorWarning', False))}/{fmt_bool(safe(cs, 'errorPassive', False))} | "
        f"REC/TEC={safe(cs, 'receiveErrorCnt', 0)}/{safe(cs, 'transmitErrorCnt', 0)} | "
        f"RX/TX/FWD={safe(cs, 'totalRxCnt', 0)}/{safe(cs, 'totalTxCnt', 0)}/{safe(cs, 'totalFwdCnt', 0)}"
      )
  add("")

  add("[2] raw CAN 8초 수신량")
  if not bus_frame_counts:
    add("CAN 프레임 수신 없음")
  else:
    for bus in sorted(bus_frame_counts):
      count = bus_frame_counts[bus]
      rate = count / elapsed
      add(f"source {bus}: {count} frames | {rate:.1f} frames/sec")
      top = bus_address_counts[bus].most_common(8)
      if top:
        add("  상위 ID: " + ", ".join(f"0x{addr:X}={cnt}" for addr, cnt in top))
  add("")

  add("[3] 현대 SCC/FCA 메시지 관측")
  any_scc = False
  for bus in sorted(scc_counts):
    entries = []
    for addr, cnt in sorted(scc_counts[bus].items()):
      any_scc = True
      entries.append(f"{HYUNDAI_SCC_IDS.get(addr, hex(addr))}=0x{addr:X}:{cnt}")
    if entries:
      add(f"source {bus}: " + " | ".join(entries))
  if not any_scc:
    add("알려진 SCC11/SCC12/SCC13/SCC14/FCA11 ID 관측 없음")
  add("")

  add("[4] carState · 주행상태")
  if last_car is None:
    add("carState: 수신 없음")
  else:
    cruise = safe(last_car, "cruiseState", None)
    add(
      f"valid={fmt_bool(valid(sm, 'carState'))} | gear={enum_name(safe(last_car, 'gearShifter', 'unknown'))} | "
      f"speed={fmt_float(float(safe(last_car, 'vEgo', 0.0)) * 3.6, 1)} km/h"
    )
    add(
      f"gasPressed={fmt_bool(safe(last_car, 'gasPressed', False))} | "
      f"brakePressed={fmt_bool(safe(last_car, 'brakePressed', False))} | "
      f"cruiseAvailable={fmt_bool(safe(cruise, 'available', False))} | "
      f"cruiseEnabled={fmt_bool(safe(cruise, 'enabled', False))}"
    )
  add("")

  add("[5] selfdrive · controls")
  if last_selfdrive is None:
    add("selfdriveState: 수신 없음")
  else:
    add(
      f"selfdriveState valid={fmt_bool(valid(sm, 'selfdriveState'))} | "
      f"enabled={fmt_bool(safe(last_selfdrive, 'enabled', False))} | "
      f"active={fmt_bool(safe(last_selfdrive, 'active', False))} | "
      f"state={enum_name(safe(last_selfdrive, 'state', 'unknown'))}"
    )
    alert = safe(last_selfdrive, "alertText1", "") or safe(last_selfdrive, "alertType", "")
    if alert:
      add(f"alert={alert}")
  if last_controls is None:
    add("controlsState: 수신 없음")
  else:
    add(
      f"controlsState valid={fmt_bool(valid(sm, 'controlsState'))} | "
      f"enabled={fmt_bool(safe(last_controls, 'enabled', False))} | "
      f"longControlState={enum_name(safe(last_controls, 'longControlState', 'unknown'))}"
    )
  add("")

  add("[6] Radar")
  if last_radar is None:
    add("radarState: 수신 없음")
  else:
    lead = safe(last_radar, "leadOne", None)
    add(f"radarState valid={fmt_bool(valid(sm, 'radarState'))}")
    if lead is not None:
      add(
        f"leadOne status={fmt_bool(safe(lead, 'status', False))} | "
        f"dRel={fmt_float(safe(lead, 'dRel', None), 1)} m | "
        f"vRel={fmt_float(safe(lead, 'vRel', None), 1)} m/s"
      )
  add("")

  add("[7] 서비스 수신 현황")
  for service in services:
    add(f"{service}: updates={service_updates[service]} | valid={fmt_bool(valid(sm, service))}")
  add("")

  add("[8] 핵심 판정")
  if "interruptRateCan2" in all_faults:
    add("[주행 금지] interruptRateCan2 감지: CAN2 인터럽트 부하/포워딩을 우선 점검해야 합니다.")
  elif all_faults:
    add("[주행 금지] Panda fault 감지: " + ", ".join(all_faults))
  elif rx_invalid:
    add("[주행 금지] Panda RX safety check가 invalid 상태입니다.")
  elif not can_seen:
    add("[주행 금지] 8초 동안 raw CAN 프레임을 수신하지 못했습니다.")
  elif not car_valid:
    add("[주의] CAN은 수신되지만 carState가 정상 valid가 아닙니다.")
  elif panda_ok:
    add("[정상 후보] 활성 Panda fault가 없고 raw CAN 및 carState가 확인됩니다.")
  else:
    add("[주의] 추가 확인이 필요합니다.")

  add("")
  add("※ 이 진단은 상태 확인용이며 안전 제한이나 Panda fault를 우회하지 않습니다.")

  report = "\n".join(lines)
  print(report)
  return 0 if verdict == "[정상 후보]" else 1


if __name__ == "__main__":
  raise SystemExit(main())
