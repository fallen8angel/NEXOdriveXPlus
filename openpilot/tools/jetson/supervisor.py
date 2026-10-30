"""Supervise only the HUD renderer, independently of network/GPU services."""
import os
from pathlib import Path
import subprocess
import time

from openpilot.tools.jetson.process import stop_process
from openpilot.tools.jetson.state import RUNTIME, atomic_json, read_fresh

STARTUP_LIMITS = {'process_start': 15., 'adapter_import': 30., 'ownership_wait': 10.,
                  'usb_open': 20., 'renderer_start': 30.}
TRANSIENT_PANEL_ERRORS = {'panel_busy', 'usb_open_failed', 'usb_write_failed', 'usb_disconnected',
                          'panel_output_stalled', 'encoder_output_stalled'}


def source_identity():
  try:
    return (Path(__file__).resolve().parents[3] / 'SOURCE_COMMIT').read_text().strip()
  except OSError:
    return None


class RendererSupervisor:
  def __init__(self, command, requested, clock=time.monotonic, spawn=subprocess.Popen, device_ready=lambda: True):
    self.command, self.requested, self.clock, self.spawn = command, requested, clock, spawn
    self.device_ready = device_ready
    self.waiting_panel = False
    self.child = None
    self.attempts = self.failures = self.successes = self.consecutive = 0
    self.next_attempt = 0.
    self.started = self.last_panel = self.healthy_since = None
    self.input_since = None
    self.confirmed = False
    self.error = ''
    self.rearm = False
    self.was_wanted = None
    self.was_ready = None
    self.retry_after = 0.
    self.timed_probes = 0
    self.error_time = None
    self.error_stage = ''
    self.last_successful_panel = None
    previous = read_fresh(RUNTIME / 'hud-recovery.json', float('inf'), now=self.clock()) or {}
    for key in ('attempts', 'failures', 'successes'):
      value = previous.get(key)
      if type(value) is int and value >= 0:
        setattr(self, key, value)
    value = previous.get('consecutive_failures')
    if type(value) is int and 0 <= value <= 3:
      self.consecutive = value
    if self.consecutive >= 3:
      # One migration probe for old permanently blocked journals, then only a
      # new data/device recovery edge can rearm. Restarts preserve the cooldown.
      self.rearm = previous.get('rearm', 'retry_after' not in previous) is True
    self.retry_after = max(self.clock(), previous.get('retry_after', self.clock())
                           if type(previous.get('retry_after')) in (int, float) else self.clock())
    self.error = str(previous.get('last_error', ''))[:240]
    self.error_stage = str(previous.get('failure_stage', ''))[:64]
    self.error_time = previous.get('last_error_time_unix')
    probes = previous.get('timed_probes', 0)
    self.timed_probes = probes if type(probes) is int and 0 <= probes <= 2 else 2
    self.last_successful_panel = previous.get('last_successful_panel_mono')
    self.source_commit = source_identity()
    if self.source_commit and self.source_commit != previous.get('source_commit'):
      # A repaired release gets one admission, even if the old release was
      # blocked by an import failure. Same-release restarts retain the budget.
      self.rearm = True
      self.retry_after = self.clock()
      self.timed_probes = 0

  def tick(self):
    now = self.clock()
    wanted = self.requested()
    try:
      ready = self.device_ready()
    except (OSError, RuntimeError):
      ready = False
    if (self.was_wanted is False and wanted) or (self.was_ready is False and ready):
      self.rearm = True
      self.timed_probes = 0
    self.was_wanted, self.was_ready = wanted, ready
    self.waiting_panel = not ready
    self.input_since = (self.input_since if self.input_since is not None else now) if wanted else None
    stage = {}
    if self.child is not None:
      exited = self.child.poll() is not None
      stage = read_fresh(RUNTIME / 'hud-stage.json', float('inf'), now=now) or {}
      if stage.get('pid') != self.child.pid or stage.get('updated', 0) < self.started:
        stage = {}
      panel = read_fresh(RUNTIME / 'hud-status.json', 2, now=now) or {}
      stamp = panel.get('last_panel_send_mono')
      valid = (type(stamp) in (int, float) and self.started <= stamp <= now
               and panel.get('pid') == self.child.pid and now - stamp < 2)
      if valid:
        self.last_panel = stamp
        self.last_successful_panel = stamp
        self.healthy_since = self.healthy_since if self.healthy_since is not None else now
        if now - self.healthy_since >= 2 and not self.confirmed:
          if self.attempts > 1:
            self.successes += 1
          self.confirmed = True
        if now - self.healthy_since >= 30:
          self.consecutive = 0
          self.timed_probes = 0
      else:
        self.healthy_since = None
      # An admitted renderer must keep sending safe waiting frames during an
      # input gap too. Missing input must not hide a frozen encoder/USB writer.
      hung = now - max(self.started, self.last_panel or self.started) >= 10
      # Lock handoff (up to 8 s), imports and first GPU/encoder setup are not
      # steady-state output stalls. A stage can extend startup once, with an
      # absolute 60 s cap; repeated heartbeats cannot keep a hung child alive.
      if self.last_panel is None and stage.get('stage') in STARTUP_LIMITS:
        hung = (now - stage['updated'] >= STARTUP_LIMITS[stage['stage']]
                or now - self.started >= 60)
      if exited or hung:
        code = self.child.returncode
        failed = hung or code not in (0, 75) or (wanted and code == 0)
        reason = read_fresh(RUNTIME / 'hud-exit.json', 30, now=now) or {}
        reason = reason if reason.get('pid') == self.child.pid else {}
        if stop_process(self.child, 'renderer'):
          self.child = None
          if failed:
            self.failures += 1
            self.consecutive = min(3, self.consecutive + 1)
            self.error = 'panel_output_stalled' if hung else reason.get('reason') or ('panel_busy' if code == 76 else 'renderer_exited')
            self.error_stage = stage.get('stage', reason.get('stage', 'process_start'))
            self.error_time = reason.get('error_time_unix') or time.time()
            if self.consecutive >= 3:
              self.retry_after = now + (300 if self.timed_probes else 60)
              self.rearm = False
          elif code == 75:
            self.error = reason.get('reason') or 'snapshot_expired'
            self.error_stage = reason.get('stage', 'snapshot')
            self.error_time = reason.get('error_time_unix') or time.time()
            if self.consecutive >= 3:
              self.rearm = True  # An input gap must not consume the USB probe.
          self.next_attempt = now + (min(30., 2. ** self.consecutive) if failed else 1.)
    elif (wanted and now >= self.next_attempt and
          (self.consecutive < 3 or ((self.rearm or (self.error in TRANSIENT_PANEL_ERRORS and self.timed_probes < 2))
                                    and now >= self.retry_after and now - self.input_since >= 2))):
      if self.waiting_panel:
        self.next_attempt = now + .5
      else:
        if self.consecutive >= 3 and not self.rearm:
          self.timed_probes += 1
        self.rearm = False
        self.attempts += 1
        try:
          self.child = self.spawn(self.command, stdin=subprocess.DEVNULL,
                                  # The protected release is root-owned. Vendor
                                  # USB imports open a relative log.log at
                                  # startup, so run only the renderer child in
                                  # its writable volatile HUD runtime folder.
                                  cwd=RUNTIME,
                                  # Child uses python -m: sys.path changes in
                                  # the parent are NOT inherited by subprocess.
                                  # Preserve release-root module discovery after
                                  # moving the working directory out of the
                                  # immutable protected release.
                                  env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[3]) +
                                       os.pathsep + os.environ.get('PYTHONPATH', '')},
                                  start_new_session=True)
          self.child._nexo_process_group = True
          self.started, self.last_panel, self.healthy_since = now, None, None
          self.confirmed = False
        except OSError:
          self.failures += 1
          self.consecutive = min(3, self.consecutive + 1)
          self.error = 'renderer_start_failed'
          self.error_stage = 'process_start'
          self.error_time = time.time()
          self.next_attempt = now + min(30., 2. ** self.consecutive)
          if self.consecutive >= 3:
            self.retry_after = now + 60
    state = {'updated': now, 'attempts': self.attempts, 'failures': self.failures,
             'successes': self.successes, 'consecutive_failures': self.consecutive,
             'blocked': self.consecutive >= 3 and self.child is None,
             'source_commit': self.source_commit,
             'blocked_reason': (self.error or 'retry_budget_exhausted') if self.consecutive >= 3 and self.child is None else None,
             'cooldown_s': max(0, max(self.next_attempt, self.retry_after if self.consecutive >= 3 else 0) - now),
             'retry_after': self.retry_after, 'rearm': self.rearm,
             'timed_probes': self.timed_probes, 'automatic_probes_remaining': max(0, 2 - self.timed_probes)
                if self.error in TRANSIENT_PANEL_ERRORS else 0,
             'last_error_time_unix': self.error_time, 'failure_stage': self.error_stage,
             'last_successful_panel_mono': self.last_successful_panel,
             'renderer_stage': stage.get('stage', 'process_start') if self.child else 'stopped',
             'input_requested': bool(wanted), 'device_present': ready,
             'supervisor_pid': os.getpid(),
             'last_error': self.error, 'renderer_pid': self.child.pid if self.child else None,
             'state': 'running' if self.child else 'blocked' if self.consecutive >= 3 else
                      'waiting_panel' if self.waiting_panel and wanted else 'waiting_data'}
    try:
      atomic_json(RUNTIME / 'hud-recovery.json', state)
    except OSError:
      pass
    return state

  def close(self):
    return stop_process(self.child, 'renderer')
