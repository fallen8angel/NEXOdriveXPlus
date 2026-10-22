"""Non-invasive interpretation of completed NEXO TXT collectors.

Only reads the existing report. No cereal subscription, CAN TX, Panda, Params,
USB ownership, or vehicle-control access. Missing test steps stay UNKNOWN.
"""
from __future__ import annotations

import re


def _section(report: str, number: int) -> str:
  match = re.search(rf"(?m)^\[{number}\] ", report)
  if not match:
    return ""
  start = match.start()
  following = re.search(r"(?m)^\[\d+(?:-[A-Z])?\] ", report[match.end():])
  return report[start:match.end() + following.start()] if following else report[start:]


def interpretation(report: str) -> str:
  """Human-readable evidence and limitations for a single parked capture."""
  panda = _section(report, 2)
  block = _section(report, 19)
  mode = _section(report, 15)
  buttons = _section(report, 20)
  blinker = _section(report, 17)
  parking = _section(report, 27)
  jetson = _section(report, 31)

  lines = [
    "[33] 안전 · MODE/MED · 주차센서 · Jetson 교차 판정 (읽기 전용)",
    "※ 기존 로그의 관측 품질만 평가합니다. 차량 안전 설정·제어·CAN·USB 설정은 변경하지 않습니다.",
  ]
  if "현재 fault: 없음" in panda and "8초 관측 fault: 없음" in panda:
    lines.append("Panda: 관측 구간의 fault는 없음. 이 사실만으로 모든 TX 프레임 허용을 의미하지 않습니다.")
  else:
    lines.append("Panda: fault가 없음을 확인하지 못했습니다. 본문 Panda 원시 기록을 검토하십시오.")

  positive_block = bool(re.search(r"safetyTxBlocked\s+\d+->\d+\s+delta=\+[1-9]", block))
  if positive_block:
    lines.append("Panda TX: 증가한 안전 차단 카운터가 기록됐습니다. 원인·정상 여부는 미확정이며 source192 시간 상관관계만으로 원인을 단정하지 않습니다.")
  elif "safetyTxBlocked" in block:
    lines.append("Panda TX: 카운터 기록은 있으나 본 구간의 증가 사례는 확인하지 못했습니다.")
  else:
    lines.append("Panda TX: 상세 차단 카운터 자료가 없어 송신 허용 상태를 판정할 수 없습니다.")

  if "MED/READY 관측=False" in mode and "CLU11 버튼 변화 관측 없음" in buttons:
    lines.append("MED: MODE/SET/RES 조작이 관측되지 않았습니다. 비활성 결과만으로 MED 고장을 판정할 수 없습니다.")
  elif "MED/READY 관측=True" in mode:
    lines.append("MED: READY 상태가 관측됐습니다. 실제 제어 전환과 조건은 본문 타임라인으로 확인하십시오.")
  else:
    lines.append("MED: 입력/전환 자료가 부족합니다. 정차 상태에서 화면·원시 버튼 수신을 분리 확인하십시오.")

  both = re.search(r"carState left=True right=True", blinker) is not None
  lone_left = re.search(r"carState left=True right=False", blinker) is not None
  lone_right = re.search(r"carState left=False right=True", blinker) is not None
  if lone_left and lone_right:
    lines.append("방향지시등: 좌·우 독립 ON 신호가 모두 기록됐습니다. UI 표시 경로는 별도 확인하십시오.")
  elif both:
    lines.append("방향지시등: 좌우 동시 ON(비상등 후보)만 확인됐습니다. 좌측·우측 개별 표시는 검증하지 못했습니다.")
  else:
    lines.append("방향지시등: 좌우 개별 신호가 모두 관측된 것은 아닙니다.")

  if "[현재 신호로 상태 판정 불가]" in parking:
    lines.append("주차센서: 후보 CAN 값이 고정되어 ON/OFF 및 단계별 전후방 경고를 확정할 수 없습니다.")
  elif "[주차센서 신호 확인]" in parking:
    lines.append("주차센서: 신호 변화가 기록됐습니다. 센서 위치·방향·표시는 별도 검증이 필요합니다.")
  else:
    lines.append("주차센서: 상태 확인에 충분한 증거가 없습니다.")

  if "enabled=False" in jetson:
    lines.append("Jetson: 외부 추론 OFF입니다. 기능 비활성과 USB 물리 미연결, Jetson 서버 오류는 서로 다른 상태입니다.")
  elif "enabled=True" in jetson:
    lines.append("Jetson: 외부 추론 설정은 ON이지만 HELLO/모델 계약/최근 추론을 각각 확인해야 합니다.")
  else:
    lines.append("Jetson: 기능 ON/OFF 확인이 부족합니다.")
  lines.append("검사 권고: 차량이 안전하게 정차했을 때 좌·우 방향지시등을 각각 기록하고, 주차센서 OFF/ON을 구분해 수집하십시오. 실주행 제어 시험의 근거로 사용하지 마십시오.")
  return "\n".join(lines)
