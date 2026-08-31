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
MESSAGE_NAMES = ("SPAS12", "PAS11")
DISPLAY_SIGNALS = {
  "SPAS12": (
    "CF_Spas_FIL_Ind", "CF_Spas_FIR_Ind", "CF_Spas_FOL_Ind", "CF_Spas_FOR_Ind",
    "CF_Spas_RIL_Ind", "CF_Spas_RIR_Ind", "CF_Spas_ROL_Ind", "CF_Spas_ROR_Ind",
    "CF_Spas_FI_Ind", "CF_Spas_RI_Ind", "CF_Spas_FLS_Alarm", "CF_Spas_FCS_Alarm",
    "CF_Spas_FRS_Alarm", "CF_Spas_FR_Alarm", "CF_Spas_RR_Alarm", "CF_Spas_RLS_Alarm",
    "CF_Spas_RCS_Alarm", "CF_Spas_BEEP_Alarm", "CF_Spas_StatAlarm",
  ),
  "PAS11": (
    "CF_Gway_PASDisplayFLH", "CF_Gway_PASDisplayFRH", "CF_Gway_PASDisplayFCTR",
    "CF_Gway_PASDisplayRLH", "CF_Gway_PASDisplayRRH", "CF_Gway_PASDisplayRCTR",
    "CF_Gway_PASFsound", "CF_Gway_PASRsound", "CF_Gway_PASSystemOn",
    "CF_Gway_PASCheckSound", "CF_Gway_PASDistance",
  ),
}


def safe(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


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


def resolve_messages(cp):
  if cp is None:
    return "", {}, ""

  fp_value = safe(cp, "carFingerprint", "")
  fingerprint = getattr(fp_value, "name", "") or str(fp_value or "")
  fingerprint = fingerprint.split(".")[-1]
  if not fingerprint:
    return "", {}, ""

  dbc_name = ""
  for key in (fp_value, fingerprint):
    try:
      dbc_name = DBC[key][Bus.pt]
      if dbc_name:
        break
    except Exception:
      pass
  if not dbc_name and fingerprint == "HYUNDAI_NEXO_1ST_GEN":
    dbc_name = "hyundai_kia_generic"
  if not dbc_name:
    return "", {}, fingerprint

  try:
    probe = CANParser(dbc_name, [], 0)
    addresses = {}
    for name in MESSAGE_NAMES:
      msg = probe.dbc.name_to_msg.get(name)
      if msg is not None:
        addresses[name] = int(msg.address)
    return dbc_name, addresses, fingerprint
  except Exception:
    return dbc_name, {}, fingerprint


def format_values(name, values):
  return " ".join(f"{signal}={int(values.get(signal, 0))}" for signal in DISPLAY_SIGNALS[name])


def verdict_lines(addresses_resolved, any_raw, any_payload_change, any_decoded_change, any_nonzero):
  if any_decoded_change:
    return ("  [주차센서 신호 확인] 위치별 표시·경고 값이 실제로 변했습니다.",)
  if any_nonzero:
    return ("  [주차센서 활성 후보] 주차센서 관련 값은 수신됐지만 8초 동안 단계 변화는 없었습니다.",)
  if any_payload_change:
    return ("  [DBC 재확인 필요] 원본 데이터는 변했지만 현재 DBC의 주차센서 값 변화로 해독되지 않았습니다.",)
  if any_raw:
    return (
      "  [현재 신호로 상태 판정 불가] SPAS12·PAS11 후보 메시지는 수신됐지만 원본·해독 값이 고정돼 있었습니다.",
      "  이 결과만으로 주차센서가 꺼졌거나 주차 버튼을 누르지 않았다고 판단할 수 없습니다.",
      "  Auto Helper 같은 외부 모듈의 자동 버튼 동작은 현재 DBC 후보 신호에 반영되지 않을 수 있습니다.",
      "  주차센서 OFF·ON 상태를 나눠 수집해 실제 NEXO 신호 주소·비트를 다시 확인하십시오.",
    )
  if not addresses_resolved:
    return ("  [DBC 확인 필요] SPAS12·PAS11 주소를 해석하지 못했습니다.",)
  return ("  [버스/메시지 확인 필요] SPAS12·PAS11 원본 CAN을 관측하지 못했습니다.",)


def main() -> int:
  sm = messaging.SubMaster(["carParams", "can"])
  params = Params()
  cp = get_car_params(sm, params)
  dbc_name, addresses, fingerprint = resolve_messages(cp)

  start = time.monotonic()
  deadline = start + OBSERVE_SECONDS
  parsers = {}
  raw_counts = Counter()
  payload_values = defaultdict(set)
  decoded_values = defaultdict(set)
  timelines = defaultdict(list)
  previous_payload = {}
  previous_decoded = {}
  decode_errors = []

  while time.monotonic() < deadline:
    sm.update(50)
    now = time.monotonic() - start

    if not addresses:
      cp_now = get_car_params(sm, params)
      if cp_now is not None:
        dbc_name, addresses, fingerprint = resolve_messages(cp_now)
    if not addresses or not sm.updated.get("can", False):
      continue

    grouped = defaultdict(list)
    try:
      address_to_name = {address: name for name, address in addresses.items()}
      for frame in list(sm["can"]):
        address = int(safe(frame, "address", -1))
        name = address_to_name.get(address)
        if name is None:
          continue
        src = int(safe(frame, "src", -1))
        dat = bytes(safe(frame, "dat", b""))
        grouped[(name, src)].append((address, dat, src))
        raw_counts[(name, src)] += 1

        payload = dat.hex().upper()
        payload_values[(name, src)].add(payload)
        if previous_payload.get((name, src)) != payload and len(timelines[(name, src)]) < 80:
          timelines[(name, src)].append(f"{now:5.2f}s raw={payload}")
        previous_payload[(name, src)] = payload

      for (name, src), frames in grouped.items():
        parser = parsers.get((name, src))
        if parser is None:
          parser = CANParser(dbc_name, [(name, 10)], src)
          parsers[(name, src)] = parser
        parser.update([[int(sm.logMonoTime["can"]), frames]])
        values = tuple(int(parser.vl[name].get(signal, 0)) for signal in DISPLAY_SIGNALS[name])
        decoded_values[(name, src)].add(values)
        if previous_decoded.get((name, src)) != values and len(timelines[(name, src)]) < 80:
          timelines[(name, src)].append(f"{now:5.2f}s {format_values(name, parser.vl[name])}")
        previous_decoded[(name, src)] = values
    except Exception as e:
      if len(decode_errors) < 10:
        decode_errors.append(f"{now:5.2f}s {type(e).__name__}: {e}")

  print("")
  print("[27] 전·후방 주차센서 CAN 후보 신호 진단")
  print("※ 안전하게 정차한 상태에서 계기판·주차 버튼의 켜짐 표시를 확인하고, 가능하면 R단에서 장애물과의 거리를 바꾸며 실행하십시오.")
  print("※ 크루즈 buttonEvents와 주차 버튼은 별도이므로 수동 주차 버튼 입력 흔적 유무를 판정에 사용하지 않습니다.")
  print("※ SPAS12·PAS11은 현재 DBC 후보이며, 값이 0이라는 이유만으로 실제 주차센서가 꺼졌다고 판단하지 않습니다.")
  print("※ 읽기 전용 진단이며 CAN·UDS를 송신하거나 차량 설정을 변경하지 않습니다.")
  print(f"fingerprint={fingerprint or '-'} | dbc={dbc_name or '-'}")

  for name in MESSAGE_NAMES:
    address = addresses.get(name)
    print("")
    print(f"  [{name}] address={'-' if address is None else f'0x{address:X}'}")
    keys = sorted(key for key in raw_counts if key[0] == name)
    if not keys:
      print("  원본 CAN 관측 없음")
      continue
    for _, src in keys:
      key = (name, src)
      print(
        f"  src {src}: RX={raw_counts[key]} | payload 종류={len(payload_values[key])} | "
        f"해독 상태 종류={len(decoded_values[key])}"
      )
      for row in timelines[key][:30]:
        print("   " + row)

  if decode_errors:
    print("")
    print("  [해독 오류 일부]")
    for row in decode_errors:
      print("   " + row)

  any_raw = bool(raw_counts)
  any_payload_change = any(len(values) > 1 for values in payload_values.values())
  any_decoded_change = any(len(values) > 1 for values in decoded_values.values())
  any_nonzero = any(any(value != 0 for value in state) for states in decoded_values.values() for state in states)

  print("")
  print("  [자동 판정]")
  for line in verdict_lines(bool(addresses), any_raw, any_payload_change, any_decoded_change, any_nonzero):
    print(line)

  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("")
    print("[27] 전·후방 주차센서 CAN 후보 신호 진단")
    print(f"주차센서 진단 내부 오류: {type(e).__name__}: {e}")
    raise SystemExit(0)
