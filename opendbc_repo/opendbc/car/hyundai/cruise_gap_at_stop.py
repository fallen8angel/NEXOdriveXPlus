"""Feedback-driven GAP requests; owns no CAN messages or longitudinal settings."""
from enum import Enum, auto
import math


class GapState(Enum):
  IDLE = auto()
  WAIT_STOP = auto()
  REDUCING = auto()
  WAIT_START = auto()
  RESTORING = auto()
  LOCKOUT = auto()


class CruiseGapAtStop:
  STOP_SECONDS = 1.0
  START_SECONDS = 0.3
  BUTTON_INTERVAL = 1.0  # Respect NEXO's existing one-second burst cooldown.
  FEEDBACK_TIMEOUT = 1.5
  OPERATION_TIMEOUT = 10.0
  MAX_ATTEMPTS = 6

  def __init__(self, log=print):
    self.log = log
    self.state = GapState.IDLE
    self.saved_gap = None
    self.stop_since = None
    self.move_since = None
    self.started = None
    self.pending_gap = None
    self.last_press = -math.inf
    self.attempts = 0

  def cancel(self, reason):
    if self.state not in (GapState.IDLE, GapState.LOCKOUT):
      self.log(f"[GAP AUTO] cancelled: {reason}")
    self.state = GapState.LOCKOUT
    self.saved_gap = self.stop_since = self.move_since = self.started = self.pending_gap = None
    self.attempts = 0

  def _begin(self, state, now, gap):
    self.state = state
    self.started = now
    self.attempts = 0
    action = "reducing" if state == GapState.REDUCING else "restoring"
    target = 1 if state == GapState.REDUCING else self.saved_gap
    self.log(f"[GAP AUTO] {action} gap {gap} -> {target}")

  def update(self, now, *, enabled, valid, gap, speed, standstill, manual_gap=False,
             cancel=False, can_send=True):
    """Return one press request, only when the caller can transmit it this tick."""
    if not enabled or not valid or cancel or manual_gap or isinstance(gap, bool) or gap not in (1, 2, 3, 4):
      reason = "manual gap button" if manual_gap else "disabled, invalid vehicle state or cruise cancelled"
      self.cancel(reason)
      return False
    if not math.isfinite(speed) or speed < 0:
      self.cancel("invalid speed")
      return False

    stopped = standstill and speed <= 0.1
    if not standstill and speed > 0.3:
      if self.move_since is None:
        self.move_since = now
    else:
      self.move_since = None
    moving = self.move_since is not None and now - self.move_since >= self.START_SECONDS

    if self.state == GapState.LOCKOUT:
      # Never fight a manual choice or retry a failure during the same stop.
      if moving:
        self.state = GapState.IDLE
      return False

    if self.state in (GapState.IDLE, GapState.WAIT_STOP):
      if not stopped:
        self.state = GapState.IDLE
        self.stop_since = None
        return False
      if self.stop_since is None:
        self.stop_since = now
        self.state = GapState.WAIT_STOP
      if now - self.stop_since < self.STOP_SECONDS:
        return False
      self.saved_gap = gap
      self.log(f"[GAP AUTO] stop detected; saved gap={gap}")
      if gap == 1:
        self.state = GapState.WAIT_START
      else:
        self._begin(GapState.REDUCING, now, gap)

    if self.state in (GapState.REDUCING, GapState.WAIT_START) and moving:
      self.log("[GAP AUTO] vehicle moving")
      self._begin(GapState.RESTORING, now, gap)

    if self.state == GapState.RESTORING and stopped:
      self.cancel("stopped again during restore")
      return False

    if self.state == GapState.WAIT_START:
      return False

    if now - self.started >= self.OPERATION_TIMEOUT:
      self.cancel("operation timeout")
      return False
    if self.pending_gap is not None:
      if gap != self.pending_gap:
        self.pending_gap = None
      elif now - self.last_press >= self.FEEDBACK_TIMEOUT:
        self.cancel("no gap feedback")
        return False
      else:
        return False

    target = 1 if self.state == GapState.REDUCING else self.saved_gap
    if gap == target:
      if self.state == GapState.REDUCING:
        self.log("[GAP AUTO] gap reached 1")
        self.state = GapState.WAIT_START
      else:
        self.log(f"[GAP AUTO] restore complete gap={gap}")
        self.state = GapState.IDLE
        self.saved_gap = self.stop_since = self.started = None
        self.attempts = 0
      return False

    if self.attempts >= self.MAX_ATTEMPTS:
      self.cancel("maximum gap button attempts")
      return False
    if self.state == GapState.REDUCING and not stopped:
      return False  # Motion must be confirmed before selecting the restore target.
    if not can_send or now - self.last_press < self.BUTTON_INTERVAL:
      return False
    self.last_press = now
    self.pending_gap = gap
    self.attempts += 1
    return True
