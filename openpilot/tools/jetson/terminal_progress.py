"""Best-effort CLI progress. A slow SSH terminal must not delay safety checks."""
from contextlib import contextmanager
import os
from queue import Empty, Full, Queue
from threading import Event, Thread


@contextmanager
def terminal_progress():
  pending = Queue(maxsize=16)
  done = Event()

  def publish(message):
    try:
      pending.put_nowait(message)
    except Full:
      pass  # The authoritative result/status file still receives each update.

  def write():
    while not done.is_set() or not pending.empty():
      try:
        message = pending.get(timeout=0.1)
      except Empty:
        continue
      try:
        os.write(2, (message + '\n').encode())
      except OSError:
        return

  thread = Thread(target=write, daemon=True)
  thread.start()
  try:
    yield publish
  finally:
    done.set()
    thread.join(timeout=0.25)
