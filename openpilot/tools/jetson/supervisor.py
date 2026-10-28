"""Supervise only the HUD renderer, independently of network/GPU services."""
import os
from pathlib import Path
import subprocess
import time

from openpilot.tools.jetson.process import stop_process
from openpilot.tools.jetson.state import RUNTIME, atomic_json, read_fresh


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
    previous = read_fresh(RUNTIME / 'hud-recovery.json', float('inf'), now=self.clock()) or {}
    for key in ('attempts', 'failures', 'successes'):
      value = previous.get(key)
      if type(value) is int and value >= 0:
        setattr(self, key, value)
    value = previous.get('consecutive_failures')
    if type(value) is int and 0 <= value <= 3:
      self.consecutive = value

  def tick(self):
    now = self.clock()
    wanted = self.requested()
    self.input_since = (self.input_since if self.input_since is not None else now) if wanted else None
    if self.child is not None:
      exited = self.child.poll() is not None
      panel = read_fresh(RUNTIME / 'hud-status.json', 2, now=now) or {}
      stamp = panel.get('last_panel_send_mono')
      valid = (type(stamp) in (int, float) and self.started <= stamp <= now
               and panel.get('pid') == self.child.pid and now - stamp < 2)
      if valid:
        self.last_panel = stamp
        self.healthy_since = self.healthy_since if self.healthy_since is not None else now
        if now - self.healthy_since >= 2 and not self.confirmed:
          if self.attempts > 1:
            self.successes += 1
          self.confirmed = True
        if now - self.healthy_since >= 30:
          self.consecutive = 0
      else:
        self.healthy_since = None
      # Missing input is not an unresponsive output process. Require ten
      # continuous seconds of requested fresh data before escalating locally.
      hung = (wanted and now - max(self.started, self.last_panel or self.started,
                                  self.input_since) >= 10)
      if exited or hung:
        failed = hung or self.child.returncode not in (0, 75) or (wanted and self.child.returncode == 0)
        if stop_process(self.child, 'renderer'):
          self.child = None
          if failed:
            self.failures += 1
            self.consecutive += 1
            self.error = 'panel_output_stalled' if hung else 'renderer_exited'
          self.next_attempt = now + (min(30., 2. ** self.consecutive) if failed else .5)
    elif wanted and self.consecutive < 3 and now >= self.next_attempt:
      try:
        self.waiting_panel = not self.device_ready()
      except (OSError, RuntimeError):
        self.waiting_panel = True
      if self.waiting_panel:
        self.next_attempt = now + .5
      else:
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
          self.consecutive += 1
          self.error = 'renderer_start_failed'
          self.next_attempt = now + min(30., 2. ** self.consecutive)
    state = {'updated': now, 'attempts': self.attempts, 'failures': self.failures,
             'successes': self.successes, 'consecutive_failures': self.consecutive,
             'blocked': self.consecutive >= 3, 'cooldown_s': max(0, self.next_attempt - now),
             'last_error': self.error, 'renderer_pid': self.child.pid if self.child else None,
             'state': 'blocked' if self.consecutive >= 3 else 'running' if self.child else
                      'waiting_panel' if self.waiting_panel and wanted else 'waiting_data'}
    try:
      atomic_json(RUNTIME / 'hud-recovery.json', state)
    except OSError:
      pass
    return state

  def close(self):
    return stop_process(self.child, 'renderer')
