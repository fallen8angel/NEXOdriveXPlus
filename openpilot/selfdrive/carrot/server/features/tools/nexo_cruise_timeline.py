#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict

REPO_ROOT = "/data/openpilot"
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from openpilot.cereal import messaging

OBSERVE_SECONDS = 8.0
CLU11 = 0x4F1
SCC_IDS = {0x420: "SCC11", 0x421: "SCC12", 0x389: "SCC14", 0x38D: "FCA11"}


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def enum_name(value):
  try:
    return str(value).split(".")[-1]
  except Exception:
    return str(value)


def bool_text(value):
  return "True" if bool(value) else "False"


def payload_hex(frame):
  data = safe(frame, "dat", b"")
  try:
    return bytes(data).hex().upper()
  except Exception:
    return str(data)


def cruise_snapshot(last):
  cs = last.get("carState")
  ss = last.get("selfdriveState")
  ct = last.get("controlsState")
  ps = last.get("pandaStates")

  cruise = safe(cs, "cruiseState", None) if cs is not None else None
  speed_kph = float(safe(cruise, "speed", 0.0) or 0.0) * 3.6
  available = bool(safe(cruise, "available", False))
  enabled = bool(safe(cruise, "enabled", False))

  self_enabled = bool(safe(ss, "enabled", False))
  self_active = bool(safe(ss, "active", False))
  self_state = enum_name(safe(ss, "state", "unknown"))

  controls_enabled = bool(safe(ct, "enabled", False))
  long_state = enum_name(safe(ct, "longControlState", "unknown"))

  controls_allowed = False
  if ps is not None:
    try:
      if len(ps):
        controls_allowed = bool(safe(ps[0], "controlsAllowed", False))
    except Exception:
      pass

  if not available:
    phase = "OFF"
  elif available and not enabled:
    phase = "MED_WAIT_CANDIDATE" if self_active or self_enabled else "CRUISE_READY"
  else:
    phase = "SPEED_CONTROL"

  target = "--" if speed_kph < 0.5 else f"{speed_kph:.1f}"
  return (
    phase,
    available,
    enabled,
    target,
    self_enabled,
    self_active,
    self_state,
    controls_enabled,
    long_state,
    controls_allowed,
  )


def snapshot_text(snapshot):
  phase, available, enabled, target, self_enabled, self_active, self_state, controls_enabled, long_state, controls_allowed = snapshot
  return (
    f"phase={phase} | cruise.available={bool_text(available)} | cruise.enabled={bool_text(enabled)} | "
    f"target={target} km/h | selfdrive.enabled={bool_text(self_enabled)} | selfdrive.active={bool_text(self_active)} | "
    f"selfdrive.state={self_state} | controls.enabled={bool_text(controls_enabled)} | longControlState={long_state} | "
    f"controlsAllowed={bool_text(controls_allowed)}"
  )


def main() -> int:
  services = ["carState", "selfdriveState", "controlsState", "pandaStates", "can", "sendcan"]
  sm = messaging.SubMaster(services)

  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS
  last = {}
  timeline = []
  previous_snapshot = None
  previous_clu_payload = {}
  clu_changes = []
  clu_counts = Counter()
  scc_tx_counts = Counter()
  scc_first = {}
  scc_last = {}
  scc_change_counts = Counter()
  scc_previous = {}
  service_updates = Counter()

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

    snapshot = cruise_snapshot(last)
    if snapshot != previous_snapshot:
      timeline.append(f"{now:5.2f}s STATE {snapshot_text(snapshot)}")
      previous_snapshot = snapshot

    try:
      if sm.updated["can"]:
        for frame in list(sm["can"]):
          src = int(safe(frame, "src", -1))
          address = int(safe(frame, "address", -1))
          if address != CLU11:
            continue
          clu_counts[src] += 1
          payload = payload_hex(frame)
          if previous_clu_payload.get(src) != payload:
            if len(clu_changes) < 80:
              clu_changes.append(f"{now:5.2f}s CLU11_RX src={src} data={payload}")
            previous_clu_payload[src] = payload
    except Exception:
      pass

    try:
      if sm.updated["sendcan"]:
        for frame in list(sm["sendcan"]):
          address = int(safe(frame, "address", -1))
          if address not in SCC_IDS:
            continue
          src = int(safe(frame, "src", -1))
          key = (src, address)
          payload = payload_hex(frame)
          scc_tx_counts[key] += 1
          scc_first.setdefault(key, payload)
          scc_last[key] = payload
          if scc_previous.get(key) != payload:
            scc_change_counts[key] += 1
            scc_previous[key] = payload
    except Exception:
      pass

  elapsed = max(0.001, time.monotonic() - start)

  print("")
  print("[12] AI 비교용 MODE · MED · 속도설정 타임라인")
  print("※ AI 목표 흐름: OFF → MODE 입력 → MED 대기(조향 활성/목표속도 --) → SET·RES 입력 → 속도제어")
  print("※ 아래 STATE는 값이 바뀐 순간만 기록합니다.")
  if timeline:
    for row in timeline[:80]:
      print("  " + row)
  else:
    print("  상태 변화 관측 없음")

  print("")
  print("[13] MODE/크루즈 버튼 원시 CLU11 변화")
  if clu_counts:
    print("  CLU11 RX 수신량: " + " | ".join(f"src {src}={count}" for src, count in sorted(clu_counts.items())))
  else:
    print("  CLU11 RX 관측 없음")
  if clu_changes:
    for row in clu_changes[:60]:
      print("  " + row)
  else:
    print("  CLU11 payload 변화 없음")

  print("")
  print("[14] openpilot SCC 송신 요약")
  if scc_tx_counts:
    for (src, address), count in sorted(scc_tx_counts.items()):
      name = SCC_IDS[address]
      print(
        f"  {name} 0x{address:X} src={src}: {count}회 ({count/elapsed:.1f}/sec) | "
        f"payload변화={scc_change_counts[(src, address)]} | first={scc_first[(src, address)]} | last={scc_last[(src, address)]}"
      )
  else:
    print("  SCC11/SCC12/SCC14/FCA11 sendcan 송신 관측 없음")

  print("")
  print("[15] AI 방식 비교 자동판정")
  phases = [row.split("phase=", 1)[1].split(" |", 1)[0] for row in timeline if "phase=" in row]
  saw_off = "OFF" in phases
  saw_wait = "MED_WAIT_CANDIDATE" in phases or "CRUISE_READY" in phases
  saw_speed = "SPEED_CONTROL" in phases
  print(f"  OFF 관측={bool_text(saw_off)} | MED/READY 관측={bool_text(saw_wait)} | SPEED_CONTROL 관측={bool_text(saw_speed)}")
  if saw_wait and saw_speed:
    print("  [AI 흐름 후보] 준비 상태와 속도제어 상태가 분리되어 관측되었습니다.")
  elif saw_wait and not saw_speed:
    print("  [대기 상태] MED/크루즈 준비 상태는 보였지만 8초 내 목표속도 활성은 관측되지 않았습니다.")
  elif saw_speed and not saw_wait:
    print("  [확인 필요] 준비 상태 없이 바로 속도제어로 전환된 것으로 보입니다.")
  else:
    print("  [재검 필요] MODE/SET/RES 입력을 포함해 다시 기록하면 상태 전환을 비교할 수 있습니다.")

  print("")
  print("[16] 타임라인 서비스 수신 현황")
  for service in services:
    print(f"  {service}: updates={service_updates[service]}")

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
