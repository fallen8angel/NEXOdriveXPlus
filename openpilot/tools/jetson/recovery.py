"""Serial USB HUD sessions. Wait for hardware before allocating a renderer."""
import logging
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json, public_text

log = logging.getLogger(__name__)


def run_hud_sessions(run, ready, release, clock=time.monotonic, sleep=time.sleep):
  failures = attempts = 0
  last_error = ''
  while True:
    try:
      atomic_json(RUNTIME / 'hud-recovery.json', {'updated': clock(), 'attempts': attempts,
                  'failures': failures, 'last_error': last_error})
    except OSError:
      pass
    if not ready():
      sleep(.5)
      continue
    started = clock()
    attempts += 1
    try:
      run()
    except SystemExit as exc:
      if exc.code not in (None, 0):
        raise
    except Exception as exc:
      last_error = public_text(f'{type(exc).__name__}: {exc}')
      log.warning('HUD session failed: %s', last_error)
      failures = (0 if clock() - started >= 30 else failures) + 1
    else:
      failures = 0
    finally:
      try:
        (RUNTIME / 'hud-status.json').unlink(missing_ok=True)
      except OSError:
        pass
    # The exception and run() stack are gone here. Do not retain traceback
    # cycles during the reconnect wait, and never initialize concurrent owners.
    release()
    delay = 60. if failures >= 3 else min(8., 2. ** failures)
    try:
      atomic_json(RUNTIME / 'hud-recovery.json', {'updated': clock(), 'attempts': attempts,
                  'failures': failures, 'last_error': last_error, 'cooldown_s': delay})
    except OSError:
      pass
    sleep(delay)
