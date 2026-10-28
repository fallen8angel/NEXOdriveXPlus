"""Bounded shutdown for optional display children; never propagate wait timeouts."""
import logging
import os
import signal
import subprocess
import threading
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json

log = logging.getLogger(__name__)
_lock = threading.Lock()
_pending = {}
_errors = 0


def children_reaping():
  with _lock:
    return bool(_pending)


def _record(role, stage):
  global _errors
  _errors += 1
  log.warning('%s shutdown: %s', role, stage)
  try:
    atomic_json(RUNTIME / f'{role}-shutdown.json', {
      'updated': time.monotonic(), 'errors': _errors, 'role': role, 'stage': stage})
  except OSError:
    pass


def stop_process(process, role='display', timeout=.2):
  """TERM, bounded wait, KILL, bounded wait, then one owned background reaper.

  Return False while a child is still being reaped. Callers must not replace
  that child until poll() confirms exit. No PID-only signals or detached children.
  """
  if process is None:
    return True
  def group_cleanup():
    if getattr(process, '_nexo_process_group', False) and hasattr(os, 'killpg'):
      try:
        os.killpg(process.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      except OSError:
        _record(role, 'group_cleanup_error')
  with _lock:
    if id(process) in _pending:
      return False
    if process.poll() is not None:
      group_cleanup()
      return True
    for signal_name in ('terminate', 'kill'):
      try:
        if getattr(process, '_nexo_process_group', False) and hasattr(os, 'killpg'):
          os.killpg(process.pid, signal.SIGTERM if signal_name == 'terminate' else signal.SIGKILL)
        else:
          getattr(process, signal_name)()
        process.wait(timeout=timeout)
        group_cleanup()
        return True
      except ProcessLookupError:
        if process.poll() is not None:
          group_cleanup()
          return True
      except subprocess.TimeoutExpired:
        _record(role, signal_name + '_timeout')
      except OSError:
        _record(role, signal_name + '_error')
    # Keep a strong reference until wait reaps it, without blocking HUD/network.
    _pending[id(process)] = process
    def reap():
      try:
        process.wait()
        group_cleanup()
      except OSError:
        _record(role, 'reap_error')
      finally:
        with _lock:
          _pending.pop(id(process), None)
    threading.Thread(target=reap, name=f'{role}-reaper', daemon=True).start()
    return False
