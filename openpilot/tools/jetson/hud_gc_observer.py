"""Low-overhead, read-only Python GC diagnostics for the Jetson HUD child.

Never collect garbage, tune GC thresholds, monkeypatch renderer, or block the
render/USB threads on file I/O. Counters run inside gc.callbacks; snapshots are
written from a dedicated daemon thread once per 5 seconds.
"""
from __future__ import annotations

import gc
import os
from pathlib import Path
import threading
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json

STATUS = RUNTIME / 'hud-python-gc.json'
INTERVAL_S = 5.


class Observer:
  def __init__(self, status=STATUS, clock=time.monotonic):
    self.status, self.clock = Path(status), clock
    self._lock = threading.Lock()
    self._started = {}
    self._count = [0, 0, 0]
    self._pause_total_ms = 0.
    self._max_pause_ms = 0.
    self._last_report = self.clock()

  def callback(self, phase, info):
    generation = info.get('generation') if isinstance(info, dict) else None
    if type(generation) is not int or not 0 <= generation <= 2:
      return
    if phase == 'start':
      # GC and this callback run in the same Python process. Only account for
      # observed collection pauses; do not pretend to measure native CUDA work.
      with self._lock:
        self._started[generation] = self.clock()
    elif phase == 'stop':
      with self._lock:
        earlier = self._started.pop(generation, None)
        self._count[generation] += 1
        if earlier is not None:
          elapsed = self.clock() - earlier
          if 0 <= elapsed <= 60:
            pause = elapsed * 1000
            self._pause_total_ms += pause
            self._max_pause_ms = max(self._max_pause_ms, pause)

  def snapshot(self):
    now = self.clock()
    with self._lock:
      count = list(self._count)
      total, maximum = self._pause_total_ms, self._max_pause_ms
      self._count = [0, 0, 0]
      self._pause_total_ms = self._max_pause_ms = 0.
      elapsed = max(0., now - self._last_report)
      self._last_report = now
    stats = gc.get_stats()
    return {'version': 1, 'updated': now, 'pid': os.getpid(), 'monitoring_only': True,
            'gc_enabled': gc.isenabled(), 'sample_interval_s': round(elapsed, 3),
            'gc_pause_total_ms': round(total, 3), 'recent_max_pause_ms': round(maximum, 3),
            'generations': [
              {'generation': generation, 'observed_collections': count[generation],
               'total_collections': int(stats[generation].get('collections', 0)),
               'collected': int(stats[generation].get('collected', 0)),
               'uncollectable': int(stats[generation].get('uncollectable', 0))}
              for generation in range(min(3, len(stats)))],
            'note': 'HUD Python GC only; native GPU/encoder and other processes not measured.'}

  def publish(self):
    atomic_json(self.status, self.snapshot())

  def run(self, stop=None):
    stop = stop or threading.Event()
    while not stop.wait(INTERVAL_S):
      try:
        self.publish()
      except (OSError, ValueError, TypeError):
        pass  # Observability must never stop a live renderer.


_observer = None


def start():
  """Start once, only within Jetson renderer child; never on Comma or supervisor."""
  global _observer
  if _observer is not None:
    return _observer
  observer = Observer()
  gc.callbacks.append(observer.callback)
  worker = threading.Thread(target=observer.run, name='nexo-hud-gc-observer', daemon=True)
  try:
    worker.start()
  except RuntimeError:
    gc.callbacks.remove(observer.callback)
    return None
  _observer = observer
  return observer
