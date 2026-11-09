"""Read-only signal phase and standstill evidence for both live displays."""
import json
import math


def nav_phase(raw, now):
  try:
    lamp = json.loads(raw)
    phase, stamp, remain = lamp['lamp'], lamp['ts'], lamp['remain']
    if (phase in ('red', 'yellow', 'green') and type(stamp) in (int, float) and stamp > 0
        and type(remain) is int and 0 <= now - stamp < min(remain, 2.5)):
      return phase
  except (ValueError, TypeError, KeyError):
    pass
  return None


def live_signal(sm, now, nav=None):
  def fresh(name):
    stamp = getattr(sm, 'logMonoTime', {}).get(name, 0) * 1e-9
    return (getattr(sm, 'alive', {}).get(name) and getattr(sm, 'valid', {}).get(name)
            and stamp > 0 and 0 <= now - stamp < .5)

  plan = sm['longitudinalPlan'] if fresh('longitudinalPlan') else None
  phase = {'red': 1, 'green': 2, 'yellow': 3}.get(nav, 0)
  if not phase and plan is not None and plan.trafficState in (1, 2):
    phase = plan.trafficState
  stopped = False
  if phase == 1 and plan is not None and plan.trafficState == 1 and plan.shouldStop and fresh('carState') and fresh('radarState'):
    cs, rs = sm['carState'], sm['radarState']
    stopped = (cs.canValid and not cs.canTimeout and str(cs.gearShifter) == 'drive' and cs.standstill
               and math.isfinite(cs.vEgo) and abs(cs.vEgo) < .1 and not plan.hasLead
               and not rs.leadOne.status and not rs.leadTwo.status)
  return phase, bool(stopped)
