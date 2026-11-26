"""Receive-only ignition-cycle observer on Comma; independent of HUD/USB lifetime."""
import json
import os
from pathlib import Path
import time
import uuid

from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite, read_fresh

STATE = Path('/data/nexo-hud/ignition.json')
LIVE = RUNTIME / 'ignition.json'
START_WINDOW = 90.0


def durable_json(path, value):
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix('.new')
  with temporary.open('w') as output:
    json.dump(value, output, allow_nan=False)
    output.flush()
    os.fsync(output.fileno())
  os.replace(temporary, path)
  if os.name == 'posix':
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(fd)
    finally:
      os.close(fd)


class IgnitionCycles:
  def __init__(self, path=STATE, boot=None):
    self.path = path
    self.boot = boot or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    try:
      self.state = json.loads(path.read_text())
      if not isinstance(self.state, dict) or type(self.state.get('on')) is not bool:
        raise ValueError('invalid ignition history')
      if 'cycle' in self.state and (not isinstance(self.state['cycle'], str)
          or len(self.state['cycle']) != 32 or any(c not in '0123456789abcdef' for c in self.state['cycle'])
          or not isinstance(self.state.get('boot'), str) or not finite(self.state.get('started'))):
        raise ValueError('invalid ignition cycle')
    except (OSError, ValueError):
      self.state = {}
    self.off_since = None
    self.last_sample = None

  def observe(self, on, now):
    # Missing/unknown/stale panda data is never evidence of ignition OFF.
    continuous = self.last_sample is not None and 0 <= now - self.last_sample < 1.5
    self.last_sample = now if type(on) is bool else None
    if type(on) is not bool:
      self.off_since = None
      return None
    if not on:
      if self.off_since is None or not continuous:
        self.off_since = now
      if now - self.off_since >= 1.0 and self.state.get('on') is not False:
        self._save({'on': False})
      return {'on': False}
    self.off_since = None
    if self.state.get('on') is False:
      self._save({'on': True, 'cycle': uuid.uuid4().hex, 'boot': self.boot, 'started': now})
    elif self.state.get('on') is not True:
      # First installation, lost state or unobserved OFF: establish a baseline.
      # Never manufacture a new ignition cycle from a process/device boot.
      self._save({'on': True})
    state = self.state
    age = now - state.get('started', now) if state.get('boot') == self.boot else None
    cycle = state.get('cycle')
    eligible = (not state.get('inhibited') and isinstance(cycle, str) and len(cycle) == 32
                and finite(age) and 0 <= age < START_WINDOW)
    return {'on': True, 'cycle': cycle if eligible else None, 'age': age if eligible else None}

  def _save(self, value):
    # Persistence must succeed before publishing a new playback opportunity.
    durable_json(self.path, value)
    self.state = value

  def inhibit(self):
    if self.state.get('on') and not self.state.get('inhibited'):
      self._save({**self.state, 'inhibited': True})


def panda_ignition(sm, now):
  stamp = sm.logMonoTime['pandaStates'] / 1e9
  if not (sm.valid['pandaStates'] and sm.alive['pandaStates'] and 0 <= now - stamp < 1.5):
    return None
  known = [p for p in sm['pandaStates'] if str(p.pandaType) != 'unknown']
  return any(p.ignitionLine or p.ignitionCan for p in known) if known else None


def startup_metadata(sm, now, *, nexo, park_enabled):
  ignition = read_fresh(LIVE, 1.5) or {}
  def fresh(name):
    stamp = sm.logMonoTime[name] / 1e9
    return sm.valid[name] and sm.alive[name] and 0 <= now - stamp < .5
  cs, sd = sm['carState'], sm['selfdriveState']
  safe = bool(nexo and park_enabled and fresh('carState') and fresh('selfdriveState')
              and cs.canValid and not cs.canTimeout and cs.standstill and abs(cs.vEgo) < .1
              and str(cs.gearShifter) == 'park' and not cs.leftBlinker and not cs.rightBlinker
              and sd.alertText1 in ('', '기어 [P] 상태') and not sd.alertText2
              and (str(sd.alertSize) == 'none' or sd.alertText1 == '기어 [P] 상태')
              and str(sd.alertStatus) == 'normal')
  return {k: ignition.get(k) for k in ('on', 'cycle', 'age')} | {'safe': safe}


def main():
  import fcntl
  from openpilot.cereal import messaging
  os.nice(10)
  STATE.parent.mkdir(parents=True, exist_ok=True)
  with STATE.with_suffix('.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cycles = IgnitionCycles()
    sm = messaging.SubMaster(['pandaStates', 'carState'])
    while True:
      sm.update(0)
      now = time.monotonic()
      try:
        # Debounce distinct vehicle samples, not repeated reads of one packet.
        value = cycles.observe(panda_ignition(sm, now), sm.logMonoTime['pandaStates'] / 1e9)
        cs = sm['carState']
        if (value and value.get('on') and sm.valid['carState'] and sm.alive['carState']
            and 0 <= now - sm.logMonoTime['carState'] / 1e9 < .5 and cs.canValid and not cs.canTimeout
            and (str(cs.gearShifter) in ('drive', 'reverse', 'neutral') or abs(cs.vEgo) >= .1)):
          cycles.inhibit()
          value['cycle'] = None
        atomic_json(LIVE, {'updated': now, **(value or {})})
      except OSError:
        # Optional decoration must never interfere with vehicle processes.
        LIVE.unlink(missing_ok=True)
      time.sleep(.25)


if __name__ == '__main__':
  main()
