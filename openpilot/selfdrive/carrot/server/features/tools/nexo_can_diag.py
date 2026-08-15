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
RADAR_TRACK_IDS = set(range(0x500, 0x520))
UDS_IDS = {0x7D0: "RADAR_UDS_REQ", 0x7D8: "RADAR_UDS_RESP"}
HYUNDAI_IDS = {
  0x420: "SCC11", 0x421: "SCC12", 0x50A: "SCC13", 0x389: "SCC14",
  0x38D: "FCA11", 0x483: "FCA12", 0x340: "LKAS11", 0x251: "MDPS12",
}
RAW_BUTTON_IDS = {0x4F1: "CLU11", 0x3EF: "CRUISE_BUTTON_ALT", 0x391: "BCM_PO_11/LFA", 0x416: "CRUISE_BUTTON_LFA"}
WATCH_PROCS = {"card", "selfdrived", "controlsd", "radard", "radard_dpath", "pandad", "ui"}
CRITICAL_PROCS = {"card", "selfdrived", "controlsd", "pandad"}
EVENT_TYPES = ("noEntry", "warning", "userDisable", "softDisable", "immediateDisable", "permanent", "overrideLateral", "overrideLongitudinal")
MAX_RAW_SAMPLES = 12
MAX_TIMELINE = 160


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


def raw_hex(frame):
  try:
    return bytes(safe(frame, "dat", b"")).hex().upper()
  except Exception:
    return ""


def run_cmd(args, timeout=2.0):
  try:
    return subprocess.check_output(args, cwd=REPO_ROOT, stderr=subprocess.STDOUT, text=True, timeout=timeout).strip()
  except Exception as e:
    return f"실행 실패: {type(e).__name__}: {e}"


def event_label(event):
  name = enum_name(safe(event, "name", "unknown"))
  types = [t for t in EVENT_TYPES if bool(safe(event, t, False))]
  return name + ("/" + ",".join(types) if types else "")


def sample_fields(obj, names):
  out = []
  for name in names:
    v = safe(obj, name, None)
    if v is not None:
      try:
        if isinstance(v, float):
          out.append(f"{name}={v:.4f}")
        else:
          out.append(f"{name}={v}")
      except Exception:
        pass
  return " | ".join(out)


def main():
  services = [
    "pandaStates", "deviceState", "managerState", "carParams", "carState", "carControl",
    "selfdriveState", "controlsState", "longitudinalPlan", "radarState", "onroadEvents", "can", "sendcan",
  ]
  sm = messaging.SubMaster(services)
  params = Params()
  start_wall = datetime.now()
  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS

  counts = Counter(); bus_counts = Counter(); tx_bus_counts = Counter()
  addr_counts = defaultdict(Counter); tx_addr_counts = defaultdict(Counter)
  watched_rx = defaultdict(Counter); watched_tx = defaultdict(Counter)
  radar_counts = defaultdict(Counter); radar_first = {}; radar_last = {}
  uds_rx = defaultdict(Counter); uds_tx = defaultdict(Counter)
  raw_samples = defaultdict(list); tx_samples = defaultdict(list)
  button_events = Counter(); button_timeline = []; med_timeline = []; control_timeline = []
  onroad_event_counts = Counter(); last_onroad_events = []
  observed_faults = set(); observed_fault_status = set(); observed_rx_invalid = False
  controls_allowed_values = []; safety_tx_values = []; last = {}
  prev_med = None; prev_control = None

  while time.monotonic() < deadline:
    sm.update(100)
    rel = time.monotonic() - start
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
      fs = enum_name(safe(p, "faultStatus", "none"))
      if fs not in ("none", "unknown", "0"):
        observed_fault_status.add(fs)
      observed_rx_invalid |= bool(safe(p, "rxChecksInvalid", False))
      controls_allowed_values.append(bool(safe(p, "controlsAllowed", False)))
      try:
        safety_tx_values.append(int(safe(p, "safetyTxBlocked", 0)))
      except Exception:
        pass

    if sm.updated.get("carState", False):
      cs = last["carState"]; cr = safe(cs, "cruiseState", None)
      for ev in list(safe(cs, "buttonEvents", [])):
        typ = enum_name(safe(ev, "type", "unknown")); pressed = bool(safe(ev, "pressed", False))
        button_events[f"{typ}:{'pressed' if pressed else 'released'}"] += 1
        if len(button_timeline) < MAX_TIMELINE:
          button_timeline.append(f"{rel:5.2f}s {typ} {'DOWN' if pressed else 'UP'}")
      med = (
        bool(safe(cr, "available", False)), bool(safe(cr, "enabled", False)),
        bool(safe(cs, "brakePressed", False)), bool(safe(cs, "gasPressed", False)), bool(safe(cs, "regenBraking", False)),
        round(float(safe(cr, "speed", 0.0)) * 3.6, 1),
      )
      if med != prev_med and len(med_timeline) < MAX_TIMELINE:
        state = "SPEED_CONTROL" if med[0] and med[1] else "MED_WAIT" if med[0] else "OFF"
        med_timeline.append(f"{rel:5.2f}s {state} avail={med[0]} enabled={med[1]} set={med[5]:.1f} brake={med[2]} gas={med[3]} regen={med[4]}")
        prev_med = med

    cc = last.get("carControl")
    if cc is not None and sm.updated.get("carControl", False):
      act = safe(cc, "actuators", None)
      ctl = (bool(safe(cc, "latActive", False)), bool(safe(cc, "longActive", False)), round(float(safe(act, "accel", 0.0)), 3))
      if ctl != prev_control and len(control_timeline) < MAX_TIMELINE:
        control_timeline.append(f"{rel:5.2f}s latActive={ctl[0]} longActive={ctl[1]} accel={ctl[2]:.3f}")
        prev_control = ctl

    try:
      if sm.updated["onroadEvents"]:
        last_onroad_events = []
        for event in list(sm["onroadEvents"]):
          label = event_label(event); last_onroad_events.append(label); onroad_event_counts[label] += 1
    except Exception:
      pass

    try:
      if sm.updated["can"]:
        for f in list(sm["can"]):
          bus = int(safe(f, "src", -1)); addr = int(safe(f, "address", -1)); dat = raw_hex(f)
          bus_counts[bus] += 1; addr_counts[bus][addr] += 1
          if addr in HYUNDAI_IDS or addr in RAW_BUTTON_IDS:
            watched_rx[bus][addr] += 1
            key = ("RX", bus, addr)
            if len(raw_samples[key]) < MAX_RAW_SAMPLES:
              raw_samples[key].append((rel, dat))
          if addr in RADAR_TRACK_IDS:
            radar_counts[bus][addr] += 1
            radar_first.setdefault((bus, addr), rel); radar_last[(bus, addr)] = rel
            key = ("RADAR", bus, addr)
            if len(raw_samples[key]) < 3:
              raw_samples[key].append((rel, dat))
          if addr in UDS_IDS:
            uds_rx[bus][addr] += 1
            key = ("UDS_RX", bus, addr)
            if len(raw_samples[key]) < MAX_RAW_SAMPLES:
              raw_samples[key].append((rel, dat))
    except Exception:
      pass

    try:
      if sm.updated["sendcan"]:
        for f in list(sm["sendcan"]):
          bus = int(safe(f, "src", -1)); addr = int(safe(f, "address", -1)); dat = raw_hex(f)
          tx_bus_counts[bus] += 1; tx_addr_counts[bus][addr] += 1
          if addr in HYUNDAI_IDS or addr in RAW_BUTTON_IDS:
            watched_tx[bus][addr] += 1
            key = ("TX", bus, addr)
            if len(tx_samples[key]) < MAX_RAW_SAMPLES:
              tx_samples[key].append((rel, dat))
          if addr in UDS_IDS:
            uds_tx[bus][addr] += 1
            key = ("UDS_TX", bus, addr)
            if len(tx_samples[key]) < MAX_RAW_SAMPLES:
              tx_samples[key].append((rel, dat))
    except Exception:
      pass

  elapsed = max(0.001, time.monotonic() - start)
  panda = last.get("pandaStates")[0] if last.get("pandaStates") is not None and len(last.get("pandaStates")) else None
  current_faults = [] if panda is None else [enum_name(x) for x in list(safe(panda, "faults", []))]
  all_faults = sorted(set(current_faults) | observed_faults)
  current_rx_invalid = bool(safe(panda, "rxChecksInvalid", False)) if panda is not None else False
  device = last.get("deviceState"); started = bool(safe(device, "started", False)) if device is not None else False
  car_valid = bool(counts["carState"] and getattr(sm, "valid", {}).get("carState", False))
  can_seen = sum(bus_counts.values()) > 0

  manager_down = []
  ms = last.get("managerState")
  if ms is not None:
    for p in list(safe(ms, "processes", [])):
      name = str(safe(p, "name", ""))
      if name in WATCH_PROCS and bool(safe(p, "shouldBeRunning", False)) and not bool(safe(p, "running", False)):
        manager_down.append(name)
  manager_down = sorted(set(manager_down)); critical_down = [x for x in manager_down if x in CRITICAL_PROCS]

  verdict = "[정상 후보]"
  if all_faults or observed_rx_invalid or not can_seen or (started and critical_down):
    verdict = "[주행 금지]"
  elif not started or not car_valid or manager_down:
    verdict = "[주의]"

  out=[]; add=out.append
  add("="*78); add("NEXOdriveXPlus 8초 통합진단 - NexoPilot 이식용 FULL TRACE"); add("="*78)
  add(f"실행시각: {start_wall:%Y-%m-%d %H:%M:%S} | 관측시간: {elapsed:.2f}초 | 판정: {verdict}")
  add("※ 읽기 전용 진단입니다. 이 스크립트는 ECU에 UDS/CommunicationControl 명령을 새로 보내지 않습니다.")

  add("\n[1] Git · 실행 버전")
  add(f"branch={run_cmd(['git','rev-parse','--abbrev-ref','HEAD'])}")
  add(f"commit={run_cmd(['git','rev-parse','HEAD'])}")
  add("dirty=" + ("False" if run_cmd(["git","status","--porcelain"]) == "" else "True"))

  add("\n[2] 레이더 활성화 Params · 초기화 흔적")
  for k in ("EnableRadarTracks", "EnableRadarTracksResult", "ExperimentalMode", "LongitudinalPersonality", "ControlsReady", "FirmwareQueryDone"):
    try:
      raw=params.get(k, encoding="utf-8")
      add(f"{k}={raw if raw is not None else '-'}")
    except Exception as e:
      add(f"{k}=<read error {e}>")
  trace = run_cmd(["tmux","capture-pane","-p","-S","-2500","-t","comma"], timeout=4.0)
  if not trace.startswith("실행 실패:"):
    keys=("enable radar tracks","try to enable radar","diagnostic session","ecu write data by id","result=","communicationcontrol","disable ecu","0x7d0","7d0","7d8","0142","01 42")
    picked=[ln for ln in trace.splitlines() if any(k in ln.lower() for k in keys)]
    add("startup/UDS 관련 tmux 기록:")
    for ln in picked[-100:]:
      add("  "+ln)
    if not picked:
      add("  관련 문자열 없음 (부팅 로그가 이미 tmux 범위를 벗어났을 수 있음)")
  else:
    add(trace)

  add("\n[3] Radar Tracks 0x500~0x51F")
  all_radar=set()
  for bus in sorted(radar_counts):
    ids=radar_counts[bus]; all_radar |= set(ids)
    add(f"source {bus}: active={len(ids)}/32 total={sum(ids.values())} rate={sum(ids.values())/elapsed:.1f}/s")
    add("  IDs: "+", ".join(f"0x{a:03X}={ids[a]}({ids[a]/elapsed:.1f}Hz)" for a in sorted(ids)))
  missing=sorted(RADAR_TRACK_IDS-all_radar)
  add(f"전체 active={len(all_radar)}/32 | missing=" + (", ".join(f"0x{x:03X}" for x in missing) if missing else "없음"))
  for bus in sorted(radar_counts):
    for addr in sorted(radar_counts[bus]):
      samples=raw_samples.get(("RADAR",bus,addr),[])
      add(f"  bus{bus} 0x{addr:03X} first={radar_first[(bus,addr)]:.3f}s last={radar_last[(bus,addr)]:.3f}s sample="+" ; ".join(f"{t:.3f}:{d}" for t,d in samples))

  add("\n[4] UDS 0x7D0/0x7D8 - 8초 창 내 관측")
  for direction, table, samples in (("RX",uds_rx,raw_samples),("TX",uds_tx,tx_samples)):
    if not table:
      add(f"{direction}: 관측 없음 (정상적으로는 초기화가 8초진단 버튼보다 먼저 끝날 수 있음)")
    for bus in sorted(table):
      for addr,count in sorted(table[bus].items()):
        key=("UDS_RX" if direction=="RX" else "UDS_TX",bus,addr)
        add(f"{direction} bus{bus} {UDS_IDS.get(addr,hex(addr))}=0x{addr:X} count={count} raw="+" ; ".join(f"{t:.3f}:{d}" for t,d in samples.get(key,[])))

  add("\n[5] SCC/FCA/LKAS/MDPS RX·TX 원시 payload")
  for bus in sorted(set(watched_rx)|set(watched_tx)):
    for addr in sorted(set(watched_rx[bus])|set(watched_tx[bus])):
      name=HYUNDAI_IDS.get(addr,RAW_BUTTON_IDS.get(addr,f"0x{addr:X}"))
      rc=watched_rx[bus][addr]; tc=watched_tx[bus][addr]
      add(f"bus{bus} {name} 0x{addr:X}: RX={rc}({rc/elapsed:.1f}Hz) TX={tc}({tc/elapsed:.1f}Hz)")
      rs=raw_samples.get(("RX",bus,addr),[]); ts=tx_samples.get(("TX",bus,addr),[])
      if rs:
        add("  RX raw: "+" ; ".join(f"{t:.3f}:{d}" for t,d in rs))
      if ts:
        add("  TX raw: "+" ; ".join(f"{t:.3f}:{d}" for t,d in ts))
  scc12_rx=sum(watched_rx[b][0x421] for b in watched_rx); scc12_tx=sum(watched_tx[b][0x421] for b in watched_tx)
  add(f"SCC12 observed: RX={scc12_rx} TX={scc12_tx} | RX+TX 동시 관측={'YES' if scc12_rx and scc12_tx else 'NO'}")
  add("※ RX+TX 동시 관측만으로 순정/OP 중복 송신을 단정하지 않습니다. payload·bus·주기와 함께 비교하십시오.")

  add("\n[6] MED / 버튼 상태 전이")
  for row in med_timeline:
    add("  "+row)
  add("buttonEvents: "+(", ".join(f"{k}={v}" for k,v in sorted(button_events.items())) if button_events else "없음"))
  for row in button_timeline[:80]:
    add("  "+row)

  add("\n[7] carControl · longitudinal control 전이")
  for row in control_timeline:
    add("  "+row)
  cc=last.get("carControl")
  if cc is not None:
    act=safe(cc,"actuators",None)
    add(sample_fields(cc,("enabled","latActive","longActive")))
    add("actuators: "+sample_fields(act,("accel","aTarget","jerk","steeringAngleDeg","torque")))
  lp=last.get("longitudinalPlan")
  if lp is not None:
    add("longitudinalPlan: "+sample_fields(lp,("aTarget","shouldStop","allowBrake","allowThrottle","hasLead","fcw")))
  cs=last.get("carState")
  if cs is not None:
    cr=safe(cs,"cruiseState",None)
    add("carState: "+sample_fields(cs,("vEgo","brakePressed","gasPressed","regenBraking","accFaulted","steeringPressed","leftBlindspot","rightBlindspot")))
    add("cruiseState: "+sample_fields(cr,("available","enabled","standstill","speed","speedCluster")))
  sd=last.get("selfdriveState")
  if sd is not None:
    add("selfdriveState: "+sample_fields(sd,("enabled","active","state","engageable","experimentalMode")))
  ctl=last.get("controlsState")
  if ctl is not None:
    add("controlsState: "+sample_fields(ctl,("enabled","active","vCruiseDEPRECATED","longControlState","alertText1","alertText2")))
  rs=last.get("radarState")
  if rs is not None:
    for n in ("leadOne","leadTwo"):
      lead=safe(rs,n,None); add(f"radarState.{n}: "+sample_fields(lead,("status","dRel","yRel","vRel","vLead","aLeadK","modelProb","radarTrackId")))

  add("\n[8] Panda safety · fault · TX block")
  if panda is None:
    add("Panda 수신 없음")
  else:
    add(f"safety={enum_name(safe(panda,'safetyModel','unknown'))} param={safe(panda,'safetyParam','-')} controlsAllowed={b(safe(panda,'controlsAllowed',False))}")
    add(f"faults={','.join(all_faults) if all_faults else '없음'} faultStatus={enum_name(safe(panda,'faultStatus','unknown'))} rxChecksInvalid current={b(current_rx_invalid)} observed={b(observed_rx_invalid)}")
    add(f"safetyTxBlocked={safe(panda,'safetyTxBlocked','-')} ignitionLine={b(safe(panda,'ignitionLine',False))} ignitionCan={b(safe(panda,'ignitionCan',False))}")
  if controls_allowed_values:
    add(f"controlsAllowed samples False={controls_allowed_values.count(False)} True={controls_allowed_values.count(True)}")
  if safety_tx_values:
    add(f"safetyTxBlocked first={safety_tx_values[0]} last={safety_tx_values[-1]} delta={safety_tx_values[-1]-safety_tx_values[0]}")

  add("\n[9] runtime CarParams")
  cp=last.get("carParams"); source="cereal"
  if cp is None:
    try:
      raw=params.get("CarParams")
      if raw is not None:
        cp=messaging.log_from_bytes(raw,car.CarParams); source="Params/CarParams"
    except Exception:
      pass
  if cp is None:
    add("carParams 수신 없음")
  else:
    add(f"source={source} fingerprint={safe(cp,'carFingerprint','-')} brand={safe(cp,'brand','-')} flags={safe(cp,'flags','-')} extFlags={safe(cp,'extFlags','-')}")
    add(f"mass={safe(cp,'mass','-')} wheelbase={safe(cp,'wheelbase','-')} steerRatio={safe(cp,'steerRatio','-')} tireStiffnessFactor={safe(cp,'tireStiffnessFactor','-')}")
    add(f"openpilotLong={b(safe(cp,'openpilotLongitudinalControl',False))} pcmCruise={b(safe(cp,'pcmCruise',False))}")
    cfg=[]
    for i,c in enumerate(list(safe(cp,'safetyConfigs',[]))):
      cfg.append(f"#{i}:{enum_name(safe(c,'safetyModel','unknown'))}({safe(c,'safetyParam','-')})")
    add("safetyConfigs="+(" ,".join(cfg) if cfg else "없음"))

  add("\n[10] raw CAN 전체 현황")
  for bus in sorted(bus_counts):
    add(f"source {bus}: RX={bus_counts[bus]} {bus_counts[bus]/elapsed:.1f}/s | top="+", ".join(f"0x{a:X}={c}" for a,c in addr_counts[bus].most_common(20)))
  for bus in sorted(tx_bus_counts):
    add(f"source {bus}: TX={tx_bus_counts[bus]} {tx_bus_counts[bus]/elapsed:.1f}/s | top="+", ".join(f"0x{a:X}={c}" for a,c in tx_addr_counts[bus].most_common(20)))

  add("\n[11] onroadEvents · manager · 최근 오류")
  add("현재 onroadEvents: "+(" | ".join(last_onroad_events) if last_onroad_events else "없음"))
  add("8초 관측: "+(" | ".join(f"{k}={v}" for k,v in sorted(onroad_event_counts.items())) if onroad_event_counts else "없음"))
  if ms is not None:
    for p in list(safe(ms,"processes",[])):
      name=str(safe(p,"name",""))
      if name in WATCH_PROCS:
        add(f"{name}: running={b(safe(p,'running',False))} should={b(safe(p,'shouldBeRunning',False))} exit={safe(p,'exitCode','-')}")
  trace2=run_cmd(["tmux","capture-pane","-p","-S","-700","-t","comma"], timeout=3.0)
  if not trace2.startswith("실행 실패:"):
    picked=[ln for ln in trace2.splitlines() if any(k in ln.lower() for k in ("traceback","exception","error","fatal","accfault","radar","scc","fca","panda","safety"))]
    for ln in picked[-80:]:
      add("  "+ln)

  add("\n[12] 서비스 수신 현황 · 최종 판정")
  for s in services:
    add(f"{s}: updates={counts[s]} valid={b(getattr(sm,'valid',{}).get(s,False))}")
  if all_faults:
    add("[주행 금지] Panda fault: "+", ".join(all_faults))
  elif observed_rx_invalid:
    add("[주행 금지] Panda rxChecksInvalid=True 관측")
  elif not can_seen:
    add("[주행 금지] raw CAN 수신 없음")
  elif started and critical_down:
    add("[주행 금지] 핵심 프로세스 비정상: "+", ".join(critical_down))
  elif not started:
    add("[주의] deviceState.started=False")
  elif not car_valid:
    add("[주의] carState valid=False")
  elif manager_down:
    add("[주의] 프로세스 비정상: "+", ".join(manager_down))
  else:
    add("[정상 후보] Panda/CAN/핵심 프로세스/carState 기본 조건 정상")
  add(f"RadarTracks summary: {len(all_radar)}/32 | EnableRadarTracksResult={params.get_bool('EnableRadarTracksResult')}")
  add("※ 이 결과는 XPlus와 NexoPilot을 1:1 비교하기 위한 캡처입니다. 실제 기능 변경이나 ECU 재설정은 수행하지 않습니다.")

  print("\n".join(out))
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("="*78)
    print("NEXOdriveXPlus 8초 통합진단 - FULL TRACE")
    print("="*78)
    print(f"진단 스크립트 내부 오류: {type(e).__name__}: {e}")
    print("※ 오류가 있어도 TXT 다운로드를 위해 종료코드는 0으로 반환합니다.")
    raise SystemExit(0)
