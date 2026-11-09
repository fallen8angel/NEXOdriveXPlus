"""Read-only Panda bus diagnostics shared by the live page and the eight-second capture."""
from dataclasses import dataclass
import math

MAX_GAP = 1.0
RECOVERY = 2.0


@dataclass
class Bus:
  count: int
  stamp: float
  uptime: int
  state: str = 'unknown'
  rate: float | None = None
  since: float | None = None
  last_bad: float | None = None
  duration: float | None = None
  recovered: bool = False

  def update(self, health, stamp, uptime):
    dt = stamp - self.stamp
    if dt <= 0:
      return
    count = health.totalErrorCnt
    continuous = dt <= MAX_GAP and uptime >= self.uptime and count >= self.count
    if not continuous:
      self.since = self.last_bad = self.duration = None
      self.recovered = False
    self.rate = (count - self.count) / dt if continuous else None
    self.stamp, self.count, self.uptime = stamp, count, uptime
    if health.busOff or health.errorPassive or (self.rate or 0) > 0:
      self.state = 'error' if health.busOff or health.errorPassive else 'warning'
      if self.since is None:
        self.since = stamp
      self.last_bad = stamp
      self.duration = stamp - self.since
      self.recovered = False
    elif self.since is not None and stamp - self.last_bad < RECOVERY:
      self.state = 'recovering'
    else:
      if self.since is not None:
        self.recovered = True
        self.since = None
      self.state = 'normal' if continuous else 'unknown'


class CanHealth:
  def __init__(self):
    self.buses = {}
    self.missing = set()
    self.ecu = None
    self.ecu_stamp = None

  def observe(self, sm, now):
    def stamp(name):
      value = sm.logMonoTime[name] * 1e-9
      return value if sm.valid[name] and value > 0 and 0 <= now - value <= MAX_GAP else None

    if sm.updated['pandaStates']:
      t = stamp('pandaStates')
      seen = set()
      self.missing.clear()
      for index, panda in enumerate(sm['pandaStates'] if t is not None else ()):
        for bus in range(3):
          name = f'canState{bus}'
          if hasattr(panda, '_has') and not panda._has(name):
            self.missing.add((index, bus))
            continue
          health = getattr(panda, name, None)
          if health is None:
            self.missing.add((index, bus))
            continue
          key = (index, bus)
          seen.add(key)
          if key not in self.buses:
            self.buses[key] = Bus(health.totalErrorCnt, -math.inf, panda.uptime)
          self.buses[key].update(health, t, panda.uptime)
      self.buses = {key: row for key, row in self.buses.items() if key in seen}
    if sm.updated['carState']:
      cs = sm['carState']
      self.ecu_stamp = stamp('carState')
      self.ecu = None
      if self.ecu_stamp is not None and cs.canValid and not cs.canTimeout:
        # NEXO classic CAN populates these from TCS13 and MDPS12.
        self.ecu = {key: getattr(cs, key) for key in ('accFaulted', 'steerFaultTemporary')}

  def report(self, now):
    rows = []
    for (index, bus), value in sorted(self.buses.items()):
      fresh = 0 <= now - value.stamp <= MAX_GAP
      rows.append(dict(panda=index, bus=bus, total=value.count, state=value.state if fresh else 'unknown',
                       rate_hz=round(value.rate, 2) if fresh and value.rate is not None else None,
                       last_error_mono=value.last_bad, duration_s=value.duration,
                       recovered=value.recovered if fresh else None))
    for index, bus in sorted(self.missing):
      rows.append(dict(panda=index, bus=bus, total=None, state='unknown', rate_hz=None,
                       last_error_mono=None, duration_s=None, recovered=None))
    states = {row['state'] for row in rows}
    state = next((s for s in ('error', 'warning', 'recovering', 'unknown') if s in states),
                 'normal' if rows else 'unknown')
    ecu = self.ecu if self.ecu_stamp is not None and 0 <= now - self.ecu_stamp <= MAX_GAP else None
    return dict(state=state, buses=rows, ecu=ecu)
