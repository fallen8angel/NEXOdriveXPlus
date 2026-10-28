"""One panel lease for the DATA HUD and boot screen, irrespective of uid."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

DIRECTORY = Path('/run/carrot-jetlink-display')


def identity():
  stat = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()
  return {'pid': os.getpid(), 'start': stat[19],
          'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def hud_requested(directory=DIRECTORY):
  try:
    request = json.loads((directory / 'hud-request').read_text())
    stat = Path(f"/proc/{int(request['pid'])}/stat").read_text().rsplit(')', 1)[1].split()
    return (stat[0] != 'Z' and stat[19] == request['start'] and
            request['boot'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip())
  except (OSError, ValueError, KeyError, TypeError, IndexError):
    return False


@contextmanager
def panel_owner(hud=False, directory=DIRECTORY, timeout=8., clock=time.monotonic, sleep=time.sleep):
  import fcntl
  directory.mkdir(mode=0o700, parents=True, exist_ok=True)
  request = directory / 'hud-request'
  acquired = False
  # Serialize HUD request writers too: a second waiter cannot erase priority.
  with (directory / 'hud.lock').open('a') as intent, (directory / 'owner.lock').open('a') as lock:
    owns_intent = False
    try:
      if hud:
        try:
          fcntl.flock(intent, fcntl.LOCK_EX | fcntl.LOCK_NB)
          owns_intent = True
        except BlockingIOError:
          yield False
          return
        temporary = request.with_suffix('.new')
        temporary.write_text(json.dumps(identity()))
        temporary.replace(request)
      deadline = clock() + timeout
      while hud or not hud_requested(directory):
        try:
          fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
          acquired = True
          break
        except BlockingIOError:
          if not hud or clock() >= deadline:
            break
          sleep(.05)
      yield acquired
    finally:
      # Caller closes USB before leaving the lease. Never unlink owner.lock.
      if acquired:
        fcntl.flock(lock, fcntl.LOCK_UN)
      if owns_intent:
        request.unlink(missing_ok=True)
        fcntl.flock(intent, fcntl.LOCK_UN)
