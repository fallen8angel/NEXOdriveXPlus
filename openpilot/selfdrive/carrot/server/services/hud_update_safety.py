"""Read-only vehicle-state gate for Jetson HUD maintenance.

The engine may remain on, but Onroad maintenance is allowed only while real,
fresh CAN-derived carState confirms P + standstill, and both driving-control
state publishers confirm disengagement. No vehicle state, Params, CAN, or
Panda value is ever written. Unknown or stale signals fail closed.
"""
from __future__ import annotations

import math
import time

SERVICES = ('carState', 'selfdriveState', 'controlsState')
MAX_SAMPLE_AGE_S = 1.0
STABLE_PARK_S = 0.5
POLL_DEADLINE_S = 3.0


def _evaluate_park(car_state, selfdrive_state, controls_state):
  """Return an explicit rejection reason, or None when parked and disengaged."""
  if str(car_state.gearShifter).lower() != 'park':
    return '변속기가 P단이 아닙니다.'
  try:
    speed = float(car_state.vEgo)
  except (TypeError, ValueError, OverflowError):
    return '실제 차량 속도를 확인할 수 없습니다.'
  if not math.isfinite(speed) or abs(speed) > 0.05 or car_state.standstill is not True:
    return '차량 정지 상태가 확인되지 않았습니다.'
  if car_state.gasPressed is not False:
    return '가속 페달 입력이 감지되거나 확인되지 않았습니다.'
  if selfdrive_state.enabled is not False or controls_state.enabled is not False:
    return '오픈파일럿 주행 제어가 활성화되어 있거나 확인되지 않았습니다.'
  return None


def live_park_proof():
  """Require continuous fresh onroad P evidence; no cached params-only approval."""
  try:
    from openpilot.cereal import messaging
    sm = messaging.SubMaster(list(SERVICES), poll='carState')
  except Exception as error:
    return False, '차량 상태 수신기 생성 실패: ' + type(error).__name__

  deadline = time.monotonic() + POLL_DEADLINE_S
  stable_since = None
  last_reason = '차량 상태(carState/제어 상태) 수신 대기 중입니다.'
  while time.monotonic() < deadline:
    try:
      sm.update(200)
      now = time.monotonic()
      fresh = all(
        sm.seen[service] and sm.valid[service] and sm.logMonoTime[service] > 0
        and 0 <= now - sm.logMonoTime[service] * 1e-9 <= MAX_SAMPLE_AGE_S
        and 0 <= now - sm.recv_time[service] <= MAX_SAMPLE_AGE_S
        for service in SERVICES
      )
      if not fresh:
        stable_since = None
        last_reason = '차량 상태 데이터가 없거나 유효하지 않습니다. P단 확인에 실패했습니다.'
        continue
      reason = _evaluate_park(sm['carState'], sm['selfdriveState'], sm['controlsState'])
      if reason is not None:
        stable_since = None
        last_reason = reason
        # Do not sit in an approval loop if the vehicle is not actually in P.
        if sm.updated['carState']:
          return False, reason
      elif sm.updated['carState']:
        if stable_since is None:
          stable_since = now
        elif now - stable_since >= STABLE_PARK_S:
          return True, '시동 ON · P단 · 완전 정지 · 주행 제어 비활성 확인'
    except Exception as error:
      return False, '차량 상태 검사 실패: ' + type(error).__name__
  return False, last_reason


def installation_ready(params=None, park_probe=live_park_proof):
  """Offroad or live-verified P; reject contradictory flags and state changes."""
  try:
    if params is None:
      # Compiled Params is unavailable in isolated pre-build CI. Import only on-device.
      from openpilot.common.params import Params
      params = Params()
    offroad = params.get_bool('IsOffroad')
    onroad = params.get_bool('IsOnroad')
    if offroad and not onroad:
      return True, '정상 Offroad 상태'
    if not onroad or offroad:
      return False, '콤마4 주행/정차 상태가 불분명합니다.'
    approved, detail = park_probe()
    if not approved:
      return False, detail
    # The state must still be Onroad (P-only maintenance), not a transition.
    if params.get_bool('IsOffroad') or not params.get_bool('IsOnroad'):
      return False, '차량 상태가 검사 도중 변경되었습니다.'
    return True, detail
  except Exception as error:
    return False, '차량 안전 상태 확인 실패: ' + type(error).__name__
