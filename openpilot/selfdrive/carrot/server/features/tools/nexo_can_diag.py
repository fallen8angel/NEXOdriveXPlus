#!/usr/bin/env python3
from __future__ import annotations

import re
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
NEXO_SYNTHETIC_BUTTON_TX_IDS = {0x4F1: "CLU11"}
WATCH_PROCS = {"card", "selfdrived", "controlsd", "radard", "radard_dpath", "pandad", "ui"}
CRITICAL_PROCS = {"card", "selfdrived", "controlsd", "pandad"}
EVENT_TYPES = (
  "noEntry", "warning", "userDisable", "softDisable", "immediateDisable",
  "permanent", "overrideLateral", "overrideLongitudinal",
)


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


def _param_text(params, key):
  try:
    value = params.get(key)
  except Exception:
    return None
  if isinstance(value, (bytes, bytearray)):
    return bytes(value).decode("utf-8", errors="replace")
  return value


def event_label(event):
  name = enum_name(safe(event, "name", "unknown"))
  types = [event_type for event_type in EVENT_TYPES if bool(safe(event, event_type, False))]
  return name + ("/" + ",".join(types) if types else "")


def _build_noise(line: str) -> bool:
  low = line.lower()
  build_tokens = (
    "-fmax-errors=", "-werror", "-wstrict-prototypes", "-mlittle-endian",
    "-mthumb", "-nostdlib", "stm32h7x5_flash.ld", "stm32f4_flash.ld",
    "startup_panda", "panda/board/jungle", "panda/board/obj/",
  )
  real_error_tokens = ("fatal error:", "undefined reference", "collect2: error", "linker command failed")
  return any(token in low for token in build_tokens) and not any(token in low for token in real_error_tokens)


def extract_relevant_trace_lines(trace: str) -> list[str]:
  """Return real runtime/compile failures while excluding compiler command noise."""
  lines = trace.splitlines()
  picked: list[str] = []
  exception_re = re.compile(
    r"(?:^|\s)(?:AttributeError|RuntimeError|KeyError|TypeError|ValueError|AssertionError|"
    r"ImportError|ModuleNotFoundError|NameError|OSError|IOError|Exception|SystemError):"
  )
  process_failure_re = re.compile(r"\b(card|selfdrived|controlsd|radard|pandad)\b.*\b(crash|failed|failure|exited|exit code|killed)\b", re.I)

  for i, line in enumerate(lines):
    low = line.lower()
    if _build_noise(line):
      continue

    if "traceback (most recent call last):" in low:
      for row in lines[i:min(i + 18, len(lines))]:
        if not _build_noise(row):
          picked.append(row)
      continue

    if exception_re.search(line) or process_failure_re.search(line):
      picked.append(line)
      continue

    if any(token in low for token in ("fatal error:", "undefined reference", "collect2: error", "linker command failed")):
      picked.append(line)

  deduped: list[str] = []
  for line in picked:
    if not deduped or deduped[-1] != line:
      deduped.append(line)
  return deduped[-50:]


def main():
  services = [
    "pandaStates", "deviceState", "managerState", "carParams", "carState",
    "selfdriveState", "controlsState", "radarState", "onroadEvents", "can", "sendcan",
  ]
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
  tx_bus_counts = Counter()
  tx_addr_counts = defaultdict(Counter)
  tx_button_counts = defaultdict(Counter)
  button_events = Counter()
  button_timeline = []
  onroad_event_counts = Counter()
  last_onroad_events = []
  observed_faults = set()
  observed_fault_status = set()
  observed_rx_invalid = False
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
      fault_status = enum_name(safe(p, "faultStatus", "none"))
      if fault_status not in ("none", "unknown", "0"):
        observed_fault_status.add(fault_status)
      observed_rx_invalid = observed_rx_invalid or bool(safe(p, "rxChecksInvalid", False))
      controls_allowed_values.append(bool(safe(p, "controlsAllowed", False)))
      try:
        safety_tx_values.append(int(safe(p, "safetyTxBlocked", 0)))
      except Exception:
        pass

    try:
      if sm.updated["carState"]:
        cs = last["carState"]
        for ev in list(safe(cs, "buttonEvents", [])):
          typ = enum_name(safe(ev, "type", "unknown"))
          pressed = bool(safe(ev, "pressed", False))
          key = f"{typ}:{'pressed' if pressed else 'released'}"
          button_events[key] += 1
          if len(button_timeline) < 80:
            button_timeline.append(f"{now_rel:5.2f}s {typ} {'DOWN' if pressed else 'UP'}")
    except Exception:
      pass

    try:
      if sm.updated["onroadEvents"]:
        last_onroad_events = []
        for event in list(sm["onroadEvents"]):
          label = event_label(event)
          last_onroad_events.append(label)
          onroad_event_counts[label] += 1
    except Exception:
      pass

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

    try:
      if sm.updated["sendcan"]:
        for f in list(sm["sendcan"]):
          bus = int(safe(f, "src", -1))
          addr = int(safe(f, "address", -1))
          tx_bus_counts[bus] += 1
          tx_addr_counts[bus][addr] += 1
          if addr in NEXO_SYNTHETIC_BUTTON_TX_IDS:
            tx_button_counts[bus][addr] += 1
    except Exception:
      pass

  elapsed = max(0.001, time.monotonic() - start)
  total_updates = sum(counts[s] for s in services)
  messaging_unavailable = total_updates == 0
  base_updates = counts["deviceState"] + counts["managerState"] + counts["pandaStates"]

  panda_states = last.get("pandaStates")
  panda = panda_states[0] if panda_states is not None and len(panda_states) else None
  current_faults = [] if panda is None else [enum_name(x) for x in list(safe(panda, "faults", []))]
  all_faults = sorted(set(current_faults) | observed_faults)
  current_rx_invalid = bool(safe(panda, "rxChecksInvalid", False)) if panda is not None else False
  can_seen = sum(bus_counts.values()) > 0
  device = last.get("deviceState")
  started = bool(safe(device, "started", False)) if device is not None else False
  car_valid = bool(counts["carState"] and getattr(sm, "valid", {}).get("carState", False))

  ms = last.get("managerState")
  manager_down = []
  if ms is not None:
    for p in list(safe(ms, "processes", [])):
      name = str(safe(p, "name", ""))
      if name in WATCH_PROCS and bool(safe(p, "shouldBeRunning", False)) and not bool(safe(p, "running", False)):
        manager_down.append(name)
  manager_down = sorted(set(manager_down))
  critical_down = [name for name in manager_down if name in CRITICAL_PROCS]

  synthetic_button_tx = sum(sum(counter.values()) for counter in tx_button_counts.values())
  suspicious_button_tx = synthetic_button_tx > int((elapsed / 0.20) + 2)

  verdict = "[정상 후보]"
  if messaging_unavailable:
    verdict = "[진단 불가]"
  elif all_faults or observed_rx_invalid or (started and not can_seen) or (started and critical_down):
    verdict = "[주행 금지]"
  elif not started or not car_valid or manager_down or suspicious_button_tx:
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
    if messaging_unavailable:
      add("Panda: 실시간 서비스 전체가 0건이라 Panda 상태 판정 불가")
    else:
      add("Panda: 수신 없음")
  else:
    add(f"현재 fault: {', '.join(current_faults) if current_faults else '없음'}")
    add(f"8초 관측 fault: {', '.join(all_faults) if all_faults else '없음'}")
    add(f"rxChecksInvalid 현재={b(current_rx_invalid)} | 8초 관측={b(observed_rx_invalid)}")
    add("8초 faultStatus: " + (", ".join(sorted(observed_fault_status)) if observed_fault_status else "none"))
    add(f"safety={enum_name(safe(panda,'safetyModel','unknown'))}({safe(panda,'safetyParam','-')}) | controlsAllowed={b(safe(panda,'controlsAllowed',False))} | faultStatus={enum_name(safe(panda,'faultStatus','unknown'))}")
    add(f"ignitionLine={b(safe(panda,'ignitionLine',False))} | ignitionCan={b(safe(panda,'ignitionCan',False))} | interruptLoad={safe(panda,'interruptLoad','-')} | safetyTxBlocked={safe(panda,'safetyTxBlocked','-')}")
  add("")

  add("[3] 시작 조건 · manager")
  add(f"deviceState.started={b(started)} | ControlsReady={b(params.get_bool('ControlsReady'))} | FirmwareQueryDone={b(params.get_bool('FirmwareQueryDone'))}")
  add(f"CarSelected3={_param_text(params, 'CarSelected3') or '-'} | CarName={_param_text(params, 'CarName') or '-'}")
  if ms is not None:
    for p in list(safe(ms, "processes", [])):
      name = str(safe(p, "name", ""))
      if name in WATCH_PROCS:
        add(f"{name}: running={b(safe(p,'running',False))} shouldBeRunning={b(safe(p,'shouldBeRunning',False))} exitCode={safe(p,'exitCode','-')}")
  if manager_down:
    add("비정상 프로세스: " + ", ".join(manager_down))
  add("")

  add("[4] raw CAN 8초 수신량")
  if not bus_counts:
    add("raw CAN: 0 frames")
    if messaging_unavailable:
      add("※ 모든 실시간 서비스가 동시에 0건이므로 차량 CAN 고장으로 단정하지 않습니다.")
  for bus in sorted(bus_counts):
    add(f"source {bus}: {bus_counts[bus]} frames | {bus_counts[bus]/elapsed:.1f} frames/sec")
    top = addr_counts[bus].most_common(8)
    if top:
      add("  상위 ID: " + ", ".join(f"0x{a:X}={c}" for a,c in top))
  add("")

  add("[5] 현대 SCC/FCA 메시지 관측")
  if scc_counts:
    for bus in sorted(scc_counts):
      add(f"source {bus}: " + " | ".join(f"{HYUNDAI_SCC_IDS[a]}=0x{a:X}:{c}" for a,c in sorted(scc_counts[bus].items())))
  else:
    add("SCC/FCA 후보 메시지 관측 없음")
  add("")

  add("[6] runtime CarParams")
  cp = last.get("carParams")
  cp_source = "cereal"
  if cp is None:
    try:
      cp_raw = params.get("CarParams")
      if cp_raw is not None:
        cp = messaging.log_from_bytes(cp_raw, car.CarParams)
        cp_source = "Params/CarParams"
    except Exception:
      pass
  if cp is None:
    add("carParams: 수신 없음")
  else:
    add(f"source={cp_source}")
    add(f"fingerprint={safe(cp,'carFingerprint','-')} | brand={safe(cp,'brand','-')} | flags={safe(cp,'flags','-')} | extFlags={safe(cp,'extFlags','-')}")
    add(f"mass={safe(cp,'mass','-')} | wheelbase={safe(cp,'wheelbase','-')} | steerRatio={safe(cp,'steerRatio','-')} | tireStiffnessFactor={safe(cp,'tireStiffnessFactor','-')}")
    add(f"openpilotLong={b(safe(cp,'openpilotLongitudinalControl',False))} | pcmCruise={b(safe(cp,'pcmCruise',False))}")
    cfgs=[]
    for i,cfg in enumerate(list(safe(cp,'safetyConfigs',[]))):
      cfgs.append(f"#{i}:{enum_name(safe(cfg,'safetyModel','unknown'))}({safe(cfg,'safetyParam','-')})")
    add("safetyConfigs=" + (", ".join(cfgs) if cfgs else "없음"))
  add("")

  add("[7] carState · selfdrive · controls · radar · onroadEvents")
  cs = last.get("carState")
  if cs is None:
    add("carState: 수신 없음")
  else:
    cr = safe(cs, "cruiseState", None)
    add(f"carState valid={b(car_valid)} | gear={enum_name(safe(cs,'gearShifter','unknown'))} | speed={float(safe(cs,'vEgo',0.0))*3.6:.1f} km/h")
    add(f"cruise available={b(safe(cr,'available',False))} | enabled={b(safe(cr,'enabled',False))} | setSpeed={float(safe(cr,'speed',0.0))*3.6:.1f} km/h")
  for s in ("selfdriveState","controlsState","radarState"):
    add(f"{s}: updates={counts[s]} | valid={b(getattr(sm,'valid',{}).get(s,False))}")
  add("현재 onroadEvents: " + (" | ".join(last_onroad_events) if last_onroad_events else "없음"))
  if onroad_event_counts:
    add("8초 관측 onroadEvents: " + " | ".join(f"{name}={count}" for name,count in sorted(onroad_event_counts.items())))
  else:
    add("8초 관측 onroadEvents: 없음")
  add("")

  add("[8] 크루즈 버튼 · LIMIT 잠김 진단")
  add("※ 8초 동안 RES/SET/CANCEL/GAP 버튼을 눌러 확인하십시오.")
  if raw_button_counts:
    for bus in sorted(raw_button_counts):
      add(f"차량 RX button CAN source {bus}: " + " | ".join(f"{RAW_BUTTON_IDS[a]}=0x{a:X}:{c}" for a,c in sorted(raw_button_counts[bus].items())))
  else:
    add("차량 RX button CAN 후보 ID 관측 없음")
  if tx_button_counts:
    for bus in sorted(tx_button_counts):
      add(f"openpilot TX NEXO button source {bus}: " + " | ".join(f"{NEXO_SYNTHETIC_BUTTON_TX_IDS[a]}=0x{a:X}:{c}" for a,c in sorted(tx_button_counts[bus].items())))
  else:
    add("openpilot TX NEXO CLU11(0x4F1) 관측 없음")
  add(f"openpilot NEXO 합성 버튼 TX 총합={synthetic_button_tx} | 평균={synthetic_button_tx/elapsed:.2f}/sec")
  if suspicious_button_tx:
    add("[주의] NEXO 합성 CLU11 버튼 TX가 비정상적으로 많습니다. 버튼 제한 우회 여부를 확인하십시오.")
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
    picked = extract_relevant_trace_lines(trace)
    for row in picked:
      add(row)
    if not picked:
      add("최근 실제 오류/traceback 없음 (Panda 빌드 명령 문자열은 제외)")
  add("")

  add("[10] 전체 서비스 수신 현황")
  for s in services:
    add(f"{s}: updates={counts[s]} | valid={b(getattr(sm,'valid',{}).get(s,False))}")
  add(f"서비스 총 업데이트={total_updates} | 기반 서비스(device/manager/panda)={base_updates}")
  if tx_bus_counts:
    add("sendcan TX 합계: " + " | ".join(f"source {bus}={count}" for bus,count in sorted(tx_bus_counts.items())))
  if messaging_unavailable:
    add("[진단 통신 이상] 모든 cereal/msgq 서비스가 8초 동안 0건입니다.")
    add("차량 CAN/Panda 자체 고장 여부는 이 결과만으로 판정하지 않습니다.")
    process_snapshot = run_cmd(["pgrep", "-af", "manager|boardd|pandad|controlsd|card|radard|modeld"], timeout=2.0)
    add("로컬 프로세스 확인:")
    for row in (process_snapshot.splitlines() if process_snapshot else ["관련 프로세스 없음"]):
      add("  " + row)
  add("")

  add("[11] 핵심 판정")
  if messaging_unavailable:
    add("[진단 불가] openpilot 실시간 서비스 전체 수신 0건")
    add("※ raw CAN/Panda/SCC/레이더/버튼의 0건 결과는 연쇄 결과이므로 개별 고장으로 단정하지 않습니다.")
  elif all_faults:
    add("[주행 금지] Panda fault 감지: " + ", ".join(all_faults))
  elif observed_rx_invalid:
    add("[주행 금지] Panda rxChecksInvalid=True가 8초 관측 중 감지되었습니다.")
  elif started and not can_seen:
    add("[주행 금지] onroad 상태인데 raw CAN 수신 없음")
  elif started and critical_down:
    add("[주행 금지] 핵심 프로세스 비정상: " + ", ".join(critical_down))
  elif not started:
    add("[주의] deviceState.started=False: 차량 제어 활성 상태가 아니므로 CAN/롱컨 판정은 제한적입니다.")
  elif counts["carState"] == 0:
    add("[주의] started=True이지만 carState=0: manager/card 실제 traceback을 확인하십시오.")
  elif not car_valid:
    add("[주의] carState valid=False")
  elif manager_down:
    add("[주의] shouldBeRunning인데 중지된 프로세스: " + ", ".join(manager_down))
  elif suspicious_button_tx:
    add("[주의] NEXO 합성 크루즈 버튼 송신량이 과다합니다.")
  else:
    add("[정상 후보] Panda RX 안전검사 · CAN · 핵심 프로세스 · carState 수신 확인")
  if last_onroad_events:
    add("현재 onroadEvents: " + " | ".join(last_onroad_events))
  add("")
  add("※ 진단 결과가 주의/주행금지/진단불가여도 TXT 다운로드가 되도록 이 스크립트는 항상 정상 종료합니다.")
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
