#!/usr/bin/env python3
"""NEXO radar/long-control focused 8 second diagnostic.

Passive capture only. It does not send UDS, CAN, or control commands.
Besides the existing radar/Panda/long-control checks, this version records the
MED_WAIT evidence needed to diagnose the reported "speed feels locked" symptom:
- carState cruise available/enabled, vEgo, vCruise, gas/brake and latEnabled
- carControl longActive and longitudinal actuator commands
- Panda controlsAllowed/rxChecksInvalid
- decoded NEXO SCC11 MainMode_ACC/VSetDis
- decoded SCC12 ACCMode/StopReq/aReqRaw/aReqValue
- decoded SCC14 ACCMode

Run on the comma device:
  cd /data/openpilot
  PYTHONPATH=/data/openpilot python3 tools/nexo_long_diag.py

Output:
  /data/nexo-long-diag-YYYYMMDD-HHMMSS.txt
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import os
import statistics
import subprocess
import time

import openpilot.cereal.messaging as messaging

DURATION = 8.0
STATE_SAMPLE_PERIOD = 0.20
UDS_IDS = {0x7D0: "UDS_REQ", 0x7D8: "UDS_RESP"}
RADAR_IDS = set(range(0x500, 0x520))
WATCH = {
  0x389: "SCC14",
  0x38D: "FCA11",
  0x420: "SCC11",
  0x421: "SCC12",
  0x50A: "SCC13",
}
FCA12_CANDIDATES = {0x483, 0x485}
MED_DECODE_IDS = {0x389, 0x420, 0x421}


def enum_text(v):
  try:
    return str(v)
  except Exception:
    return "?"


def field(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def git_info():
  def run(args):
    try:
      return subprocess.check_output(args, cwd="/data/openpilot", text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
      return "unknown"
  return run(["git", "rev-parse", "--abbrev-ref", "HEAD"]), run(["git", "rev-parse", "HEAD"]), run(["git", "status", "--porcelain"]) != ""


def iter_can_event(evt, service):
  try:
    frames = getattr(evt, service)
  except Exception:
    return
  for m in frames:
    try:
      yield int(m.address), int(m.src), bytes(m.dat)
    except Exception:
      continue


def fmt_rate(times, duration):
  if not times:
    return "0회"
  intervals = [(b - a) * 1000.0 for a, b in zip(times, times[1:])]
  if not intervals:
    return f"{len(times)}회 ({len(times)/duration:.1f}/sec)"
  avg = statistics.fmean(intervals)
  return (f"{len(times)}회 ({len(times)/duration:.1f}/sec) | "
          f"주기 avg={avg:.2f}ms min={min(intervals):.2f}ms max={max(intervals):.2f}ms")


def le_signal(dat, start, size, factor=1.0, offset=0.0):
  """Decode the little-endian Hyundai signals used by SCC11/12/14."""
  if len(dat) < 8:
    return None
  raw = (int.from_bytes(dat, "little") >> start) & ((1 << size) - 1)
  return raw * factor + offset


def decode_med_scc(addr, dat):
  # Signal layout is from hyundai_kia_generic.dbc used by first-gen NEXO.
  if addr == 0x420:  # SCC11
    return {
      "MainMode_ACC": int(le_signal(dat, 0, 1) or 0),
      "VSetDis": float(le_signal(dat, 8, 8) or 0.0),
    }
  if addr == 0x421:  # SCC12
    return {
      "ACCMode": int(le_signal(dat, 13, 2) or 0),
      "StopReq": int(le_signal(dat, 15, 1) or 0),
      "aReqRaw": float(le_signal(dat, 24, 11, 0.01, -10.23) or 0.0),
      "aReqValue": float(le_signal(dat, 37, 11, 0.01, -10.23) or 0.0),
    }
  if addr == 0x389:  # SCC14
    return {"ACCMode": int(le_signal(dat, 32, 3) or 0)}
  return None


def format_decoded(addr, decoded):
  if not decoded:
    return ""
  if addr == 0x420:
    return f"MainMode_ACC={decoded['MainMode_ACC']} VSetDis={decoded['VSetDis']:.0f}"
  if addr == 0x421:
    return (f"ACCMode={decoded['ACCMode']} StopReq={decoded['StopReq']} "
            f"aReqRaw={decoded['aReqRaw']:+.2f} aReqValue={decoded['aReqValue']:+.2f}")
  if addr == 0x389:
    return f"ACCMode={decoded['ACCMode']}"
  return str(decoded)


def main():
  started_wall = datetime.now()
  out_path = f"/data/nexo-long-diag-{started_wall:%Y%m%d-%H%M%S}.txt"
  branch, commit, dirty = git_info()

  can_sock = messaging.sub_sock("can", conflate=False)
  sendcan_sock = messaging.sub_sock("sendcan", conflate=False)
  services = ["pandaStates", "carState", "carControl", "selfdriveState", "controlsState", "radarState", "onroadEvents"]
  sm = messaging.SubMaster(services)

  raw_times = defaultdict(list)
  raw_payloads = defaultdict(Counter)
  uds_timeline = []
  radar_first = {}
  radar_counts = Counter()
  panda_timeline = []
  fault_timeline = []
  event_timeline = []
  state_timeline = []
  med_scc_timeline = []
  med_scc_values = defaultdict(list)
  last_med_can = {}
  last_panda = None
  last_acc_fault = None
  last_events = None
  next_state_sample = 0.0
  current_med = False

  t0 = time.monotonic()
  while time.monotonic() - t0 < DURATION:
    now = time.monotonic() - t0
    sm.update(0)

    if sm.updated.get("pandaStates", False):
      ps_list = sm["pandaStates"]
      if len(ps_list):
        p = ps_list[0]
        snap = (
          enum_text(field(p, "safetyModel", "?")),
          field(p, "safetyParam", "?"),
          bool(field(p, "controlsAllowed", False)),
          bool(field(p, "rxChecksInvalid", False)),
          enum_text(field(p, "faultStatus", "?")),
          int(field(p, "safetyTxBlocked", 0) or 0),
        )
        if snap != last_panda:
          panda_timeline.append((now, snap))
          last_panda = snap

    if sm.updated.get("carState", False):
      cs = sm["carState"]
      af = field(cs, "accFaulted", None)
      if af != last_acc_fault:
        fault_timeline.append((now, af, float(field(cs, "vEgo", 0.0) or 0.0)))
        last_acc_fault = af

    cs = sm["carState"]
    cc = sm["carControl"]
    cruise = field(cs, "cruiseState", None)
    available = bool(field(cruise, "available", False))
    enabled = bool(field(cruise, "enabled", False))
    current_med = available and not enabled

    if now >= next_state_sample:
      actuators = field(cc, "actuators", None)
      state_timeline.append({
        "t": now,
        "available": available,
        "enabled": enabled,
        "med": current_med,
        "vEgo": float(field(cs, "vEgo", 0.0) or 0.0) * 3.6,
        "vCruise": float(field(cs, "vCruise", 0.0) or 0.0),
        "cruiseSpeed": float(field(cruise, "speed", 0.0) or 0.0) * 3.6,
        "latEnabled": bool(field(cs, "latEnabled", False)),
        "gas": bool(field(cs, "gasPressed", False)),
        "brake": bool(field(cs, "brakePressed", False)),
        "longActive": bool(field(cc, "longActive", False)),
        "accel": float(field(actuators, "accel", 0.0) or 0.0),
        "aTarget": float(field(actuators, "aTarget", 0.0) or 0.0),
        "longState": enum_text(field(actuators, "longControlState", "?")),
      })
      next_state_sample += STATE_SAMPLE_PERIOD

    if sm.updated.get("onroadEvents", False):
      names = []
      try:
        for e in sm["onroadEvents"]:
          name = enum_text(field(e, "name", "?"))
          types = ",".join(enum_text(x) for x in field(e, "types", []))
          names.append(f"{name}/{types}")
      except Exception:
        pass
      snap = tuple(sorted(names))
      if snap != last_events:
        event_timeline.append((now, snap))
        last_events = snap

    for service, sock in (("can", can_sock), ("sendcan", sendcan_sock)):
      for evt in messaging.drain_sock(sock):
        for addr, src, dat in iter_can_event(evt, service):
          key = (service, src, addr)
          raw_times[key].append(now)
          raw_payloads[key][dat.hex().upper()] += 1

          if addr in UDS_IDS:
            uds_timeline.append((now, service, src, addr, dat.hex().upper()))
          if addr in RADAR_IDS:
            radar_counts[(service, src, addr)] += 1
            radar_first.setdefault((service, src, addr), now)

          if current_med and service == "sendcan" and addr in MED_DECODE_IDS:
            decoded = decode_med_scc(addr, dat)
            if decoded is not None:
              med_scc_values[addr].append(decoded)
              snap = tuple(sorted(decoded.items()))
              key2 = (src, addr)
              if last_med_can.get(key2) != snap:
                med_scc_timeline.append((now, src, addr, dat.hex().upper(), decoded))
                last_med_can[key2] = snap

    time.sleep(0.002)

  lines = []
  add = lines.append
  add("=" * 72)
  add("NEXOdriveXPlus 8초 레이더 · 롱컨 · MED 속도잠김 집중진단")
  add("=" * 72)
  add(f"실행시각: {started_wall:%Y-%m-%d %H:%M:%S}")
  add(f"관측시간: {DURATION:.2f}초")
  add("")
  add("[1] Git · 실행 버전")
  add(f"branch: {branch}")
  add(f"commit: {commit}")
  add(f"dirty: {dirty}")

  add("")
  add("[17] 레이더 UDS 0x7D0/0x7D8 타임라인")
  if uds_timeline:
    for t, service, src, addr, dat in uds_timeline[:160]:
      add(f"{t:6.3f}s {service.upper():7s} src={src:<3d} {UDS_IDS[addr]} 0x{addr:03X} data={dat}")
  else:
    add("8초 동안 0x7D0/0x7D8 관측 없음")
    add("※ 부팅 전에 UDS가 끝났다면 이 결과만으로 UDS 미실행을 뜻하지 않습니다.")

  add("")
  add("[18] 레이더 트랙 0x500~0x51F 최초 등장 · 물리 버스")
  if radar_counts:
    by_src = Counter()
    first_by_src = {}
    ids_by_src = defaultdict(set)
    for (service, src, addr), cnt in radar_counts.items():
      by_src[(service, src)] += cnt
      ids_by_src[(service, src)].add(addr)
      ft = radar_first[(service, src, addr)]
      first_by_src[(service, src)] = min(first_by_src.get((service, src), ft), ft)
    for key in sorted(by_src, key=lambda k: first_by_src[k]):
      service, src = key
      ids = sorted(ids_by_src[key])
      add(f"{service} src={src}: first={first_by_src[key]:.3f}s | frames={by_src[key]} | IDs={len(ids)}/32 | range=0x{ids[0]:03X}~0x{ids[-1]:03X}")
  else:
    add("레이더 트랙 0x500~0x51F 관측 없음")

  add("")
  add("[19] SCC/FCA takeover · 주기 · 중복 송신 진단")
  all_ids = dict(WATCH)
  for a in FCA12_CANDIDATES:
    all_ids[a] = "FCA12후보"
  for addr, name in all_ids.items():
    found = []
    for (service, src, a), times in raw_times.items():
      if a == addr and times:
        found.append((service, src, times, raw_payloads[(service, src, a)]))
    if not found:
      add(f"{name} 0x{addr:03X}: 관측 없음")
      continue
    for service, src, times, payloads in sorted(found, key=lambda x: (x[0], x[1])):
      sample = next(iter(payloads)) if payloads else "-"
      add(f"{name} 0x{addr:03X} {service} src={src}: {fmt_rate(times, DURATION)} | payload종류={len(payloads)} | sample={sample}")

  coexist = []
  for addr, name in all_ids.items():
    rx = [(s, src, a) for (s, src, a) in raw_times if s == "can" and a == addr and src < 128]
    tx = [(s, src, a) for (s, src, a) in raw_times if s == "sendcan" and a == addr]
    if rx and tx:
      coexist.append((name, addr, sorted({k[1] for k in rx}), sorted({k[1] for k in tx})))
  if coexist:
    add("동시 관측 경고:")
    for name, addr, rxs, txs in coexist:
      add(f"  {name} 0x{addr:03X}: 차량RX src={rxs} + OP sendcan src={txs}")
  else:
    add("차량 RX와 openpilot sendcan의 동일 SCC/FCA 동시 관측 없음")

  add("")
  add("[20] Panda safetyModel · safetyParam · controlsAllowed 타임라인")
  if panda_timeline:
    for t, snap in panda_timeline:
      model, param, allowed, invalid, status, blocked = snap
      add(f"{t:6.3f}s safetyModel={model} | safetyParam={param} | controlsAllowed={allowed} | rxChecksInvalid={invalid} | faultStatus={status} | safetyTxBlocked={blocked}")
  else:
    add("pandaStates 변화 관측 없음")

  add("")
  add("[21] accFaulted · onroadEvents 변화")
  if fault_timeline:
    for t, af, v in fault_timeline:
      add(f"{t:6.3f}s accFaulted={af} | vEgo={v*3.6:.1f} km/h")
  else:
    add("accFaulted 필드 변화 관측 없음")
  if event_timeline:
    for t, evs in event_timeline:
      add(f"{t:6.3f}s events: " + (" | ".join(evs) if evs else "없음"))

  add("")
  add("[22] AI 비교용 자동 요약")
  uds_req = any(x[3] == 0x7D0 for x in uds_timeline)
  uds_resp = any(x[3] == 0x7D8 for x in uds_timeline)
  tracks = bool(radar_counts)
  acc_fault_true = any(x[1] is True for x in fault_timeline)
  add(f"UDS_REQ={uds_req} | UDS_RESP={uds_resp} | radarTracks={tracks} | accFaulted=True 관측={acc_fault_true}")
  if coexist and acc_fault_true:
    add("[의심] SCC/FCA takeover 중복과 accFault가 함께 관측되었습니다.")
  elif tracks and not acc_fault_true:
    add("[정상 후보] 레이더 트랙이 있고 8초 동안 accFaulted=True 전환은 보이지 않았습니다.")
  elif not tracks:
    add("[재검 필요] 레이더 트랙이 보이지 않았습니다.")

  add("")
  add("[23] MED 속도잠김 전용 상태 타임라인")
  add("※ MED 판정: cruiseState.available=True + enabled=False")
  if state_timeline:
    for s in state_timeline:
      tag = "MED" if s["med"] else ("SPEED" if s["available"] and s["enabled"] else "OFF")
      add(f"{s['t']:6.3f}s {tag:5s} | vEgo={s['vEgo']:5.1f}km/h | vCruise={s['vCruise']:6.1f} | cruiseSpeed={s['cruiseSpeed']:5.1f} | "
          f"lat={s['latEnabled']} longActive={s['longActive']} | accel={s['accel']:+.2f} aTarget={s['aTarget']:+.2f} "
          f"longState={s['longState']} | gas={s['gas']} brake={s['brake']}")
  else:
    add("carState/carControl 상태 샘플 없음")

  add("")
  add("[24] MED 중 openpilot 송신 SCC11/12/14 디코딩")
  if med_scc_timeline:
    for t, src, addr, dat, decoded in med_scc_timeline[:180]:
      add(f"{t:6.3f}s src={src:<3d} {WATCH[addr]} 0x{addr:03X} | {format_decoded(addr, decoded)} | data={dat}")
    if len(med_scc_timeline) > 180:
      add(f"... 상태 변화 {len(med_scc_timeline)-180}개 추가 생략")
  else:
    add("MED 상태에서 SCC11/12/14 sendcan 디코딩 자료 없음")

  add("")
  add("[25] MED 속도잠김 자동 판정")
  med_states = [s for s in state_timeline if s["med"]]
  if not med_states:
    add("[재검 필요] 8초 동안 MED_WAIT가 관측되지 않았습니다. MED만 켜고 속도 SET 없이 다시 진단하십시오.")
  else:
    long_active_bad = any(s["longActive"] for s in med_states)
    accel_bad = any(abs(s["accel"]) > 0.05 or abs(s["aTarget"]) > 0.05 for s in med_states)
    neutral_med = [s for s in med_states if not s["gas"] and not s["brake"]]
    add(f"MED samples={len(med_states)} | longActive=True 관측={long_active_bad} | accel/aTarget ±0.05 초과={accel_bad}")
    if neutral_med:
      add(f"페달 미조작 MED 구간 vEgo: {neutral_med[0]['vEgo']:.1f} -> {neutral_med[-1]['vEgo']:.1f} km/h (단, 경사/노면 영향은 별도)")

    scc11 = med_scc_values.get(0x420, [])
    scc12 = med_scc_values.get(0x421, [])
    scc14 = med_scc_values.get(0x389, [])
    scc11_vset = any(abs(x["VSetDis"]) > 0.1 for x in scc11)
    scc12_active = any(x["ACCMode"] != 0 or x["StopReq"] != 0 or abs(x["aReqRaw"]) > 0.05 or abs(x["aReqValue"]) > 0.05 for x in scc12)
    scc14_active = any(x["ACCMode"] != 0 for x in scc14)
    add(f"MED SCC11 VSetDis!=0 관측={scc11_vset}")
    add(f"MED SCC12 종방향 명령 관측={scc12_active} (ACCMode/StopReq/aReq 기준)")
    add(f"MED SCC14 ACCMode!=0 관측={scc14_active}")

    if long_active_bad or accel_bad or scc11_vset or scc12_active:
      add("[속도잠김 의심] MED인데 상위 롱컨 또는 SCC11/SCC12 종방향 명령이 남아 있습니다. 해당 타임라인을 우선 확인하십시오.")
    elif scc14_active:
      add("[비교 필요] 상위 롱컨과 SCC12 가감속 명령은 꺼져 있지만 SCC14 ACCMode가 유지됩니다. NEXOdriveAI MED 캡처와 동일 신호를 비교하십시오.")
    else:
      add("[정상 후보] MED에서 상위 롱컨과 SCC11/SCC12/SCC14의 속도 유지 명령이 확인되지 않았습니다. 체감 잠김이면 다른 CAN/회생제동 경로를 확인하십시오.")

  add("")
  add("권장 MED 잠김 테스트: 평지에서 MED만 켜고 SET/RES는 누르지 않은 채 가속페달에서 발을 떼고 8초 진단을 실행하십시오.")
  add("※ 이 도구는 수동 캡처 전용입니다. UDS 요청이나 CAN 제어 프레임을 송신하지 않습니다.")
  add("NEXO_LONG_DIAG_COMPLETE")

  text = "\n".join(lines) + "\n"
  try:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
      f.write(text)
  finally:
    print(text, end="")
    print(f"저장파일: {out_path}")


if __name__ == "__main__":
  main()
