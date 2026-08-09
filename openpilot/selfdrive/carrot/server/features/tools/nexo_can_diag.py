#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

REPO_ROOT = "/data/openpilot"
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from openpilot.cereal import car, messaging
from openpilot.common.params import Params

OBSERVE_SECONDS = 8.0
HYUNDAI_SCC_IDS = {0x420: "SCC11", 0x421: "SCC12", 0x50A: "SCC13", 0x389: "SCC14", 0x38D: "FCA11"}
RAW_BUTTON_IDS = {0x4F1: "CLU11", 0x3EF: "CRUISE_BUTTON_ALT", 0x391: "BCM_PO_11/LFA", 0x416: "CRUISE_BUTTON_LFA"}
WATCH_PROCS = {"card", "selfdrived", "controlsd", "radard", "radard_dpath", "pandad", "ui"}


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def enum_name(v):
  try:
    return str(v).split(".")[-1]
  except Exception:
    return str(v)


def b(v):
  return "True" if bool(v) else "False"


def run_cmd(args, timeout=2.0):
  try:
    return subprocess.check_output(args, cwd=REPO_ROOT, stderr=subprocess.STDOUT, text=True, timeout=timeout).strip()
  except Exception as e:
    return f"실행 실패: {type(e).__name__}: {e}"


def main():
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
  raw_button_counts = defaultdict(Counter)
  button_events = Counter()
  button_timeline = []
  observed_faults = set()
  controls_allowed_values = []
  safety_tx_values = []
  last = {}

  while time.monotonic() < deadline:
    sm.update(100)
    now_rel = time.monotonic() - start
    for s in services:
      try:
        if sm.updated[s]:
          counts[s] += 1
          last[s] = sm[s]
      except Exception:
        pass

    ps = last.get("pandaStates")
    if ps is not None and len(ps):
      p = ps[0]
      for fault in list(safe(p, "faults", [])):
        observed_faults.add(enum_name(fault))
      controls_allowed_values.append(bool(safe(p, "controlsAllowed", False)))
      try:
        safety_tx_values.append(int(safe(p, "safetyTxBlocked", 0)))
      except Exception:
        pass

    if "carState" in last and counts["carState"]:
      cs = last["carState"]
      for ev in list(safe(cs, "buttonEvents", [])):
        typ = enum_name(safe(ev, "type", "unknown"))
        pressed = bool(safe(ev, "pressed", False))
        key = f"{typ}:{'pressed' if pressed else 'released'}"
        button_events[key] += 1
        if len(button_timeline) < 80:
          button_timeline.append(f"{now_rel:5.2f}s {typ} {'DOWN' if pressed else 'UP'}")

    try:
      if sm.updated["can"]:
        for f in list(sm["can"]):
          bus = int(safe(f, "src", -1))
          addr = int(safe(f, "address", -1))
          bus_counts[bus] += 1
          addr_counts[bus][addr] += 1
          if addr in HYUNDAI_SCC_IDS:
            scc_counts[bus][addr] += 1
          if addr in RAW_BUTTON_IDS:
            raw_button_counts[bus][addr] += 1
    except Exception:
      pass

  elapsed = max(0.001, time.monotonic() - start)
  panda = last.get("pandaStates")[0] if last.get("pandaStates") is not None and len(last.get("pandaStates")) else None
  current_faults = [] if panda is None else [enum_name(x) for x in list(safe(panda, "faults", []))]
  all_faults = sorted(set(current_faults) | observed_faults)
  can_seen = sum(bus_counts.values()) > 0
  device = last.get("deviceState")
  started = bool(safe(device, "started", False)) if device is not None else False
  car_valid = bool(counts["carState"] and getattr(sm, "valid", {}).get("carState", False))

  verdict = "[정상 후보]"
  if all_faults or not can_seen:
    verdict = "[주행 금지]"
  elif not started or not car_valid:
    verdict = "[주의]"

  out = []
  add = out.append
  add("=" * 68)
  add("NEXOdriveXPlus 8초 통합진단")
  add("=" * 68)
  add(f"실행시각: {start_wall.strftime('%Y-%m-%d %H:%M:%S')}")
  add(f"관측시간: {elapsed:.2f}초")
  add(f"판정: {verdict}")
  add("")
  add("[1] Git · 실행 버전")
  add(f"branch: {run_cmd(['git','rev-parse','--abbrev-ref','HEAD'])}")
  add(f"commit: {run_cmd(['git','rev-parse','HEAD'])}")
  dirty = run_cmd(["git","status","--porcelain"])
  add("dirty: " + ("False" if dirty == "" else "True"))
  add("")
  add("[2] Panda fault · 안전상태")
  if panda is None:
    add("Panda: 수신 없음")
  else:
    add(f"현재 fault: {', '.join(current_faults) if current_faults else '없음'}")
    add(f"8초 관측 fault: {', '.join(all_faults) if all_faults else '없음'}")
    add(f"safety={enum_name(safe(panda,'safetyModel','unknown'))}({safe(panda,'safetyParam','-')}) | controlsAllowed={b(safe(panda,'controlsAllowed',False))} | rxChecksInvalid={b(safe(panda,'rxChecksInvalid',False))} | faultStatus={enum_name(safe(panda,'faultStatus','unknown'))}")
    add(f"ignitionLine={b(safe(panda,'ignitionLine',False))} | ignitionCan={b(safe(panda,'ignitionCan',False))} | interruptLoad={safe(panda,'interruptLoad','-')} | safetyTxBlocked={safe(panda,'safetyTxBlocked','-')}")
  add("")
  add("[3] 시작 조건 · manager")
  add(f"deviceState.started={b(started)} | ControlsReady={b(params.get_bool('ControlsReady'))} | FirmwareQueryDone={b(params.get_bool('FirmwareQueryDone'))}")
  add(f"CarSelected3={params.get('CarSelected3', encoding='utf-8') or '-'} | CarName={params.get('CarName', encoding='utf-8') or '-'}")
  ms = last.get("managerState")
  if ms is not None:
    for p in list(safe(ms, "processes", [])):
      name = str(safe(p, "name", ""))
      if name in WATCH_PROCS:
        add(f"{name}: running={b(safe(p,'running',False))} shouldBeRunning={b(safe(p,'shouldBeRunning',False))} exitCode={safe(p,'exitCode','-')}")
  add("")
  add("[4] raw CAN 8초 수신량")
  for bus in sorted(bus_counts):
    add(f"source {bus}: {bus_counts[bus]} frames | {bus_counts[bus]/elapsed:.1f} frames/sec")
    top = addr_counts[bus].most_common(8)
    if top:
      add("  상위 ID: " + ", ".join(f"0x{a:X}={c}" for a,c in top))
  add("")
  add("[5] 현대 SCC/FCA 메시지 관측")
  for bus in sorted(scc_counts):
    add(f"source {bus}: " + " | ".join(f"{HYUNDAI_SCC_IDS[a]}=0x{a:X}:{c}" for a,c in sorted(scc_counts[bus].items())))
  add("")
  add("[6] runtime CarParams")
  cp = last.get("carParams")
  if cp is None:
    add("carParams: 수신 없음")
  else:
    add(f"fingerprint={safe(cp,'carFingerprint','-')} | brand={safe(cp,'brand','-')} | flags={safe(cp,'flags','-')} | extFlags={safe(cp,'extFlags','-')}")
    add(f"mass={safe(cp,'mass','-')} | wheelbase={safe(cp,'wheelbase','-')} | steerRatio={safe(cp,'steerRatio','-')} | tireStiffnessFactor={safe(cp,'tireStiffnessFactor','-')}")
    add(f"openpilotLong={b(safe(cp,'openpilotLongitudinalControl',False))} | pcmCruise={b(safe(cp,'pcmCruise',False))}")
    cfgs=[]
    for i,cfg in enumerate(list(safe(cp,'safetyConfigs',[]))):
      cfgs.append(f"#{i}:{enum_name(safe(cfg,'safetyModel','unknown'))}({safe(cfg,'safetyParam','-')})")
    add("safetyConfigs=" + (", ".join(cfgs) if cfgs else "없음"))
  add("")
  add("[7] carState · selfdrive · controls · radar")
  cs = last.get("carState")
  if cs is None:
    add("carState: 수신 없음")
  else:
    cr = safe(cs, "cruiseState", None)
    add(f"carState valid={b(car_valid)} | gear={enum_name(safe(cs,'gearShifter','unknown'))} | speed={float(safe(cs,'vEgo',0.0))*3.6:.1f} km/h")
    add(f"cruise available={b(safe(cr,'available',False))} | enabled={b(safe(cr,'enabled',False))} | setSpeed={float(safe(cr,'speed',0.0))*3.6:.1f} km/h")
  for s in ("selfdriveState","controlsState","radarState"):
    add(f"{s}: updates={counts[s]} | valid={b(getattr(sm,'valid',{}).get(s,False))}")
  add("")
  add("[8] 크루즈 버튼 · LIMIT 잠김 진단")
  add("※ 8초 동안 RES/SET/CANCEL/GAP 버튼을 눌러 확인하십시오.")
  if raw_button_counts:
    for bus in sorted(raw_button_counts):
      add(f"raw button CAN source {bus}: " + " | ".join(f"{RAW_BUTTON_IDS[a]}=0x{a:X}:{c}" for a,c in sorted(raw_button_counts[bus].items())))
  else:
    add("raw button CAN 후보 ID 관측 없음")
  add("buttonEvents: " + (", ".join(f"{k}={v}" for k,v in sorted(button_events.items())) if button_events else "없음"))
  for row in button_timeline[:40]:
    add("  " + row)
  if controls_allowed_values:
    add(f"controlsAllowed 관측: False={controls_allowed_values.count(False)} True={controls_allowed_values.count(True)}")
  if safety_tx_values:
    add(f"safetyTxBlocked: first={safety_tx_values[0]} last={safety_tx_values[-1]} delta={safety_tx_values[-1]-safety_tx_values[0]}")
  add("")
  add("[9] 최근 핵심 오류 · traceback")
  trace = run_cmd(["tmux","capture-pane","-p","-S","-500","-t","comma"], timeout=3.0)
  if trace.startswith("실행 실패:"):
    add(trace)
  else:
    picked=[ln for ln in trace.splitlines() if any(k in ln.lower() for k in ("traceback","exception","error","fatal","attributeerror","runtimeerror","keyerror","hyundai_nexo","card","selfdrived","controlsd","radard"))]
    for row in picked[-50:]:
      add(row)
    if not picked:
      add("최근 관련 오류/traceback 문자열 없음")
  add("")
  add("[10] 전체 서비스 수신 현황")
  for s in services:
    add(f"{s}: updates={counts[s]} | valid={b(getattr(sm,'valid',{}).get(s,False))}")
  add("")
  add("[11] 핵심 판정")
  if all_faults:
    add("[주행 금지] Panda fault 감지: " + ", ".join(all_faults))
  elif not can_seen:
    add("[주행 금지] raw CAN 수신 없음")
  elif not started:
    add("[주의] deviceState.started=False")
  elif counts["carState"] == 0:
    add("[주의] started=True이지만 carState=0: manager/card traceback을 확인하십시오.")
  elif not car_valid:
    add("[주의] carState valid=False")
  else:
    add("[정상 후보] onroad 시작과 carState 수신 확인")
  add("")
  add("※ 진단 결과가 주의/주행금지여도 TXT 다운로드가 되도록 이 스크립트는 항상 정상 종료합니다.")
  print("\n".join(out))
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("="*68)
    print("NEXOdriveXPlus 8초 통합진단")
    print("="*68)
    print(f"진단 스크립트 내부 오류: {type(e).__name__}: {e}")
    print("※ 오류가 있어도 TXT 다운로드를 위해 종료코드는 0으로 반환합니다.")
    raise SystemExit(0)
