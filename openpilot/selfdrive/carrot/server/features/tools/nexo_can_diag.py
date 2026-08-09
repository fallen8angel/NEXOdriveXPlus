#!/usr/bin/env python3
from __future__ import annotations

import time

import cereal.messaging as messaging


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


def main() -> int:
  sm = messaging.SubMaster(["pandaStates", "carState", "selfdriveState"])
  deadline = time.monotonic() + 3.0
  last_panda = None
  last_car = None
  last_selfdrive = None
  observed_faults: set[str] = set()

  while time.monotonic() < deadline:
    sm.update(100)
    if sm.updated.get("pandaStates", False) and len(sm["pandaStates"]):
      last_panda = sm["pandaStates"][0]
      for fault in list(safe(last_panda, "faults", [])):
        observed_faults.add(enum_name(fault))
    if sm.updated.get("carState", False):
      last_car = sm["carState"]
    if sm.updated.get("selfdriveState", False):
      last_selfdrive = sm["selfdriveState"]

  print("=" * 60)
  print("NEXO CAN 진단 (3초 관측)")
  print("=" * 60)

  if last_panda is None:
    print("판정: [주행 금지] pandaStates 수신 없음")
    print("Panda 상태를 읽지 못했습니다. pandad/CAN 연결을 확인하세요.")
    return 2

  current_faults = [enum_name(x) for x in list(safe(last_panda, "faults", []))]
  all_faults = sorted(set(current_faults) | observed_faults)
  rx_invalid = bool(safe(last_panda, "rxChecksInvalid", False))
  fault_status = enum_name(safe(last_panda, "faultStatus", "unknown"))
  controls_allowed = bool(safe(last_panda, "controlsAllowed", False))
  interrupt_load = safe(last_panda, "interruptLoad", None)

  if all_faults or rx_invalid:
    verdict = "[주행 금지]"
  elif last_car is None or not bool(sm.valid.get("carState", False)):
    verdict = "[주의]"
  else:
    verdict = "[정상 후보]"

  print(f"판정: {verdict}")
  print("현재 fault:", ", ".join(current_faults) if current_faults else "없음")
  print("3초 관측 fault:", ", ".join(all_faults) if all_faults else "없음")
  print(f"faultStatus={fault_status} | controlsAllowed={controls_allowed} | rxChecksInvalid={rx_invalid}")
  if interrupt_load is not None:
    try:
      print(f"interruptLoad={float(interrupt_load):.4f}")
    except Exception:
      print(f"interruptLoad={interrupt_load}")

  can_states = list(safe(last_panda, "canState", []))
  for i, cs in enumerate(can_states):
    print(
      f"CAN core {i}: "
      f"busOff={bool(safe(cs, 'busOff', False))} | "
      f"warn/passive={bool(safe(cs, 'errorWarning', False))}/{bool(safe(cs, 'errorPassive', False))} | "
      f"REC/TEC={safe(cs, 'receiveErrorCnt', 0)}/{safe(cs, 'transmitErrorCnt', 0)} | "
      f"irq0/irq1={safe(cs, 'irq0CallRate', 0)}/{safe(cs, 'irq1CallRate', 0)} per sec | "
      f"RX/TX/FWD={safe(cs, 'totalRxCnt', 0)}/{safe(cs, 'totalTxCnt', 0)}/{safe(cs, 'totalFwdCnt', 0)}"
    )

  if last_car is not None:
    print(
      "carState: "
      f"valid={bool(sm.valid.get('carState', False))} | "
      f"gear={enum_name(safe(last_car, 'gearShifter', 'unknown'))} | "
      f"speed={float(safe(last_car, 'vEgo', 0.0)) * 3.6:.1f} km/h | "
      f"cruiseAvailable={bool(safe(safe(last_car, 'cruiseState', None), 'available', False))}"
    )
  else:
    print("carState: 수신 없음")

  if last_selfdrive is not None:
    print(
      "selfdriveState: "
      f"enabled={bool(safe(last_selfdrive, 'enabled', False))} | "
      f"active={bool(safe(last_selfdrive, 'active', False))}"
    )

  if "interruptRateCan2" in all_faults:
    print("핵심: interruptRateCan2 감지됨 — CAN2 인터럽트 부하/포워딩을 우선 점검하세요.")
  elif all_faults:
    print("핵심: Panda fault가 감지되었습니다. fault 이름 기준으로 원인을 추적하세요.")
  elif rx_invalid:
    print("핵심: RX safety check가 invalid 상태입니다.")
  else:
    print("핵심: Panda fault와 RX safety invalid는 관측되지 않았습니다.")

  return 0 if verdict == "[정상 후보]" else 1


if __name__ == "__main__":
  raise SystemExit(main())
