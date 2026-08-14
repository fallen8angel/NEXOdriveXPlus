#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict

REPO_ROOT = "/data/openpilot"
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from openpilot.cereal import car, messaging
from openpilot.common.params import Params
from opendbc.can import CANParser
from opendbc.car import Bus
from opendbc.car.hyundai.values import DBC


OBSERVE_SECONDS = 8.0


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def b(value):
  return "True" if bool(value) else "False"


def payload_hex(frame):
  try:
    return bytes(safe(frame, "dat", b"")).hex().upper()
  except Exception:
    return str(safe(frame, "dat", ""))


def get_car_params(sm, params):
  try:
    if sm.updated.get("carParams", False):
      return sm["carParams"]
  except Exception:
    pass
  try:
    raw = params.get("CarParams")
    if raw is not None:
      return messaging.log_from_bytes(raw, car.CarParams)
  except Exception:
    pass
  return None


def resolve_cgw1(cp):
  if cp is None:
    return "", None, ""
  fingerprint = str(safe(cp, "carFingerprint", "") or "")
  if not fingerprint:
    return "", None, ""
  try:
    dbc_name = DBC[fingerprint][Bus.pt]
    probe = CANParser(dbc_name, [], 0)
    msg = probe.dbc.name_to_msg.get("CGW1")
    return dbc_name, (None if msg is None else int(msg.address)), fingerprint
  except Exception as e:
    return "", None, f"{fingerprint} ({type(e).__name__}: {e})"


def main() -> int:
  services = ["carState", "carParams", "can"]
  sm = messaging.SubMaster(services)
  params = Params()

  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS

  cp = get_car_params(sm, params)
  dbc_name, cgw1_addr, fingerprint = resolve_cgw1(cp)

  state_timeline = []
  raw_signal_timeline = []
  raw_payload_timeline = []
  prev_state = None
  prev_raw_signal = {}
  prev_payload = {}
  raw_counts = Counter()
  raw_signal_counts = defaultdict(Counter)
  left_seen = False
  right_seen = False
  hazard_seen = False
  carstate_updates = 0
  parsers = {}

  while time.monotonic() < deadline:
    sm.update(50)
    now = time.monotonic() - start

    if not dbc_name or cgw1_addr is None:
      cp_now = get_car_params(sm, params)
      if cp_now is not None:
        dbc_name, cgw1_addr, fingerprint = resolve_cgw1(cp_now)

    try:
      if sm.updated["carState"]:
        carstate_updates += 1
        cs = sm["carState"]
        state = (bool(safe(cs, "leftBlinker", False)), bool(safe(cs, "rightBlinker", False)))
        left_seen = left_seen or state[0]
        right_seen = right_seen or state[1]
        hazard_seen = hazard_seen or (state[0] and state[1])
        if state != prev_state:
          state_timeline.append(f"{now:5.2f}s carState left={b(state[0])} right={b(state[1])}")
          prev_state = state
    except Exception:
      pass

    if cgw1_addr is None or not dbc_name:
      continue

    try:
      if sm.updated["can"]:
        grouped = defaultdict(list)
        for frame in list(sm["can"]):
          address = int(safe(frame, "address", -1))
          if address != cgw1_addr:
            continue
          src = int(safe(frame, "src", -1))
          dat = bytes(safe(frame, "dat", b""))
          grouped[src].append((address, dat, src))
          raw_counts[src] += 1

          payload = payload_hex(frame)
          if prev_payload.get(src) != payload:
            if len(raw_payload_timeline) < 100:
              raw_payload_timeline.append(f"{now:5.2f}s CGW1_RX src={src} data={payload}")
            prev_payload[src] = payload

        for src, frames in grouped.items():
          parser = parsers.get(src)
          if parser is None:
            parser = CANParser(dbc_name, [("CGW1", 10)], src)
            parsers[src] = parser
          parser.update([[int(sm.logMonoTime["can"]), frames]])
          left_raw = int(parser.vl["CGW1"]["CF_Gway_TurnSigLh"])
          right_raw = int(parser.vl["CGW1"]["CF_Gway_TurnSigRh"])
          raw_signal_counts[src][f"L{left_raw}R{right_raw}"] += 1
          raw_state = (left_raw, right_raw)
          if prev_raw_signal.get(src) != raw_state:
            if len(raw_signal_timeline) < 100:
              raw_signal_timeline.append(
                f"{now:5.2f}s CGW1 src={src} CF_Gway_TurnSigLh={left_raw} CF_Gway_TurnSigRh={right_raw}"
              )
            prev_raw_signal[src] = raw_state
    except Exception as e:
      if len(raw_signal_timeline) < 100:
        raw_signal_timeline.append(f"{now:5.2f}s CGW1 decode error: {type(e).__name__}: {e}")

  print("")
  print("[17] 방향지시등 · 콤마 표시 진단")
  print("※ 8초 동안 왼쪽 ON→OFF → 오른쪽 ON→OFF 순서로 조작하면 가장 정확합니다.")
  print("※ NEXO 1세대 파서 기준: CGW1.CF_Gway_TurnSigLh/Rh → carState.leftBlinker/rightBlinker")
  print(f"carState updates={carstate_updates} | leftBlinker 관측={b(left_seen)} | rightBlinker 관측={b(right_seen)} | hazard 후보={b(hazard_seen)}")

  if state_timeline:
    print("  [carState 변화]")
    for row in state_timeline[:80]:
      print("   " + row)
  else:
    print("  carState 방향지시등 상태 변화 없음")

  print("")
  print("  [CGW1 원본 CAN/DBC]")
  print(f"  fingerprint={fingerprint or '-'} | dbc={dbc_name or '-'} | CGW1 address={'-' if cgw1_addr is None else f'0x{cgw1_addr:X}'}")
  if raw_counts:
    print("  CGW1 RX: " + " | ".join(f"src {src}={count}" for src, count in sorted(raw_counts.items())))
  else:
    print("  CGW1 raw CAN 관측 없음")

  if raw_signal_timeline:
    print("  [CGW1 방향지시등 신호 변화]")
    for row in raw_signal_timeline[:80]:
      print("   " + row)
  else:
    print("  CGW1 CF_Gway_TurnSigLh/Rh 변화 없음")

  if raw_payload_timeline:
    print("  [CGW1 payload 변화 일부]")
    for row in raw_payload_timeline[:30]:
      print("   " + row)

  print("")
  print("  [자동 판정]")
  raw_blinker_seen = any(
    key != "L0R0" and count > 0
    for counts in raw_signal_counts.values()
    for key, count in counts.items()
  )
  state_blinker_seen = left_seen or right_seen
  if raw_blinker_seen and not state_blinker_seen:
    print("  [파싱/전달 문제 후보] CGW1에서는 방향지시등 ON이 보였지만 carState에는 전달되지 않았습니다.")
  elif state_blinker_seen:
    print("  [carState 정상 후보] 방향지시등 ON이 carState까지 전달되었습니다. 화면 표시/UI 경로를 확인하십시오.")
  elif raw_counts and not raw_blinker_seen:
    print("  [입력 신호 확인 필요] CGW1은 수신됐지만 방향지시등 ON 신호가 관측되지 않았습니다. 8초 동안 좌/우를 다시 조작해 보십시오.")
  elif cgw1_addr is None:
    print("  [DBC 확인 필요] CGW1 주소를 해석하지 못했습니다.")
  else:
    print("  [버스/메시지 확인 필요] CGW1 원본 CAN을 관측하지 못했습니다.")

  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("")
    print("[17] 방향지시등 · 콤마 표시 진단")
    print(f"방향지시등 진단 내부 오류: {type(e).__name__}: {e}")
    raise SystemExit(0)
