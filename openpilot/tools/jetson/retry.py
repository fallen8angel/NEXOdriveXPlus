"""Bound optional USB attempts without rejecting a model or changing vehicle Params."""
import json
from pathlib import Path

from openpilot.tools.jetson.state import atomic_json, finite

MAX_FAILURES = 3
HEALTHY_SECONDS = 2.0


class UsbRetry:
  """Consecutive failures survive service restarts, but never a machine reboot.

  Absence is checked by the caller before begin(). A new connection must keep
  heartbeats and successful transfers for two seconds before resetting the budget.
  Exhaustion leaves USB idle for this boot; other display sources keep running.
  """
  def __init__(self, path, boot_id=None):
    self.path = Path(path)
    self.boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not self.boot_id:
      raise ValueError('USB retry requires a local boot identity')
    self.failures = 0
    self.next_attempt = 0.0
    self.pending = False
    self.confirmed = False
    self.healthy_since = None
    try:
      value = json.loads(self.path.read_text())
      if isinstance(value, dict) and value.get('boot_id') == self.boot_id:
        failures, next_attempt = value.get('failures'), value.get('next_attempt')
        if type(failures) is int and 0 <= failures <= MAX_FAILURES and finite(next_attempt) and next_attempt >= 0:
          self.failures, self.next_attempt = failures, next_attempt
        else:
          # Invalid same-boot state must not grant unlimited attempts on restart.
          self.failures = MAX_FAILURES
    except FileNotFoundError:
      pass
    except (OSError, ValueError):
      # A corrupt journal is optional-link failure, never model rejection.
      self.failures = MAX_FAILURES

  def _save(self):
    atomic_json(self.path, {'boot_id': self.boot_id, 'failures': self.failures,
                            'next_attempt': self.next_attempt})

  def ready(self, now):
    return self.failures < MAX_FAILURES and now >= self.next_attempt

  def begin(self, now):
    if not self.ready(now):
      raise RuntimeError('USB retry budget exhausted or cooling down')
    # Write before opening: a killed setup/transfer also consumes an attempt.
    self.failures += 1
    self.next_attempt = now + 2 ** (self.failures - 1)
    self.pending, self.confirmed, self.healthy_since = True, False, None
    self._save()

  def failed(self, now):
    if not self.pending:
      self.failures = min(MAX_FAILURES, self.failures + 1)
    self.next_attempt = now + 2 ** max(0, self.failures - 1)
    self.pending, self.confirmed, self.healthy_since = False, False, None
    self._save()

  def observe(self, alive, now):
    if self.confirmed:
      return
    if not alive:
      self.healthy_since = None
      return
    if self.healthy_since is None:
      self.healthy_since = now
    if now - self.healthy_since >= HEALTHY_SECONDS:
      self.failures, self.next_attempt = 0, 0.0
      self.pending, self.confirmed = False, True
      self._save()
