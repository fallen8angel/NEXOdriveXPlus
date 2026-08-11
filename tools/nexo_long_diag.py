#!/usr/bin/env python3
"""NEXO radar/long-control focused 8 second diagnostic.

Passive capture only. It does not send UDS, CAN, or control commands.
It records the evidence needed to compare NEXOdriveAI and NEXOdriveXPlus:
- radar UDS traffic around 0x7D0/0x7D8 and the physical CAN source
- radar tracks 0x500..0x51F and first-seen source/time
- stock/openpilot SCC/FCA coexistence and transition timing
- Panda safetyModel/safetyParam/controlsAllowed/rxChecksInvalid
- SCC/FCA rate, payload changes and timing jitter
- accFaulted/onroad event transition timing

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
UDS_IDS = {0x7D0: "UDS_REQ", 0x7D8: "UDS_RESP"}
RADAR_IDS = set(range(0x500, 0x520))
WATCH = {
  0x389: "SCC14",
  0x38D: "FCA11",
  0x420: "SCC11",
  0x421: "SCC12",
  0x50A: "SCC13",
}
# FCA12 differs between Hyundai generations/DBCs. Keep likely IDs visible
# without making a false decode claim.
FCA12_CANDIDATES = {0x483, 0x485}


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


def main():
  started_wall = datetime.now()
  out_path = f"/data/nexo-long-diag-{started_wall:%Y%m%d-%H%M%S}.txt"
  branch, commit, dirty = git_info()

  can_sock = messaging.sub_sock("can", conflate=False)
  sendcan_sock = messaging.sub_sock("sendcan", conflate=False)
  services = ["pandaStates", "carState", "selfdriveState", "controlsState", "radarState", "onroadEvents"]
  sm = messaging.SubMaster(services)

  raw_times = defaultdict(list)       # (service, src, addr) -> t
  raw_payloads = defaultdict(Counter) # (service, src, addr) -> payload counts
  uds_timeline = []
  radar_first = {}
  radar_counts = Counter()
  watched_timeline = []
  panda_timeline = []
  fault_timeline = []
  event_timeline = []
  last_panda = None
  last_acc_fault = None
  last_events = None

  t0 = time.monotonic()
  while time.monotonic() - t0 < DURATION:
    now = time.monotonic() - t0

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
          if addr in WATCH or addr in FCA12_CANDIDATES:
            watched_timeline.append((now, service, src, addr, dat.hex().upper()))

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

    time.sleep(0.002)

  lines = []
  add = lines.append
  add("=" * 68)
  add("NEXOdriveXPlus 8초 레이더 · 롱컨 집중진단")
  add("=" * 68)
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
    if len(uds_timeline) > 160:
      add(f"... {len(uds_timeline)-160}개 추가 프레임 생략")
  else:
    add("8초 동안 0x7D0/0x7D8 관측 없음")
    add("※ 부팅/레이더 초기화 전에 이미 UDS가 끝났다면 이 결과만으로 UDS 미실행을 뜻하지 않습니다.")

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
        payloads = raw_payloads[(service, src, a)]
        found.append((service, src, times, payloads))
    if not found:
      add(f"{name} 0x{addr:03X}: 관측 없음")
      continue
    for service, src, times, payloads in sorted(found, key=lambda x: (x[0], x[1])):
      first_payload = next(iter(payloads)) if payloads else "-"
      add(f"{name} 0x{addr:03X} {service} src={src}: {fmt_rate(times, DURATION)} | payload종류={len(payloads)} | sample={first_payload}")

  # Explicitly flag same SCC/FCA address seen both on vehicle RX and OP TX.
  coexist = []
  for addr, name in all_ids.items():
    rx_keys = [(s, src, a) for (s, src, a) in raw_times if s == "can" and a == addr and src < 128]
    tx_keys = [(s, src, a) for (s, src, a) in raw_times if s == "sendcan" and a == addr]
    if rx_keys and tx_keys:
      coexist.append((name, addr, sorted({k[1] for k in rx_keys}), sorted({k[1] for k in tx_keys})))
  if coexist:
    add("동시 관측 경고:")
    for name, addr, rxs, txs in coexist:
      add(f"  {name} 0x{addr:03X}: 차량RX src={rxs} + OP sendcan src={txs}")
    add("  ※ 실제 충돌 판정은 타임라인과 Panda safety/accFault 변화까지 함께 비교하십시오.")
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
  add(f"UDS_REQ(0x7D0)={uds_req} | UDS_RESP(0x7D8)={uds_resp} | radarTracks={tracks} | accFaulted=True 관측={acc_fault_true}")
  if coexist and acc_fault_true:
    add("[의심] SCC/FCA takeover 중복과 accFault가 함께 관측되었습니다. AI 정상 캡처와 순서 비교가 필요합니다.")
  elif tracks and not acc_fault_true:
    add("[정상 후보] 레이더 트랙이 관측되고 8초 동안 accFaulted=True 전환은 보이지 않았습니다.")
  elif not tracks:
    add("[재검 필요] 레이더 트랙이 보이지 않았습니다. 초기화 직후 또는 AI 정상 깃에서 동일 진단을 비교하십시오.")
  else:
    add("[재검 필요] UDS/SCC/FCA 타임라인을 AI 정상 캡처와 비교하십시오.")

  add("")
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
