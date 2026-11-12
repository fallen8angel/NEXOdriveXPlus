"""Read-only 5-second Jetson CPU/GPU/HUD history, separate from real-time loops."""
from __future__ import annotations

import fcntl
import threading
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json

HISTORY = RUNTIME / 'performance-history.json'
LOCK = RUNTIME / 'performance-history.lock'
MAX_SAMPLES = 48
INTERVAL_S = 5.0


def compact(sample, previous=None, *, unix_now=None):
  """Each record uses only Jetson-local measurements; no clock mixing."""
  p = sample.get('performance') or {}
  hud = p.get('hud_pipeline') or {}
  gpu_list = (p.get('clocks') or {}).get('gpu') or []
  gpu = gpu_list[0] if gpu_list and isinstance(gpu_list[0], dict) else {}
  gap_count = hud.get('panel_gap_count')
  old = (previous or {}).get('panel_gap_count')
  gap_delta = gap_count - old if type(gap_count) is int and type(old) is int and gap_count >= old else None
  threads = p.get('server_scheduler_threads') or []
  waits = [row.get('runqueue_wait_ms') for row in threads[:12] if isinstance(row, dict)]
  waits = [x for x in waits if type(x) in (int, float) and 0 <= x < 100000]
  return {
    'jetson_mono_s': p.get('sample_monotonic'),
    'jetson_unix_s': round(time.time() if unix_now is None else unix_now, 3),
    'cpu_busy_pct': (p.get('cpu_busy_pct') or {}).get('cpu'),
    'gpu_clock_mhz': gpu.get('clock_mhz'), 'gpu_load_pct': gpu.get('load_pct'),
    'power_mode': (p.get('power_mode') or {}).get('mode'),
    'memavailable_kb': (p.get('memory') or {}).get('memavailable_kb'),
    'max_runqueue_wait_ms': max(waits) if waits else None,
    'panel_gap_count': gap_count, 'panel_gap_delta': gap_delta,
    'panel_gap_max_s': hud.get('panel_gap_max_s'),
    'encoder_expired_count': hud.get('encoder_expired_count'),
    'hud_trace_age_s': p.get('hud_trace_age_s'),
    'engine_state': (p.get('engine') or {}).get('state'),
    'hud_gap_observed_in_interval': gap_delta is not None and gap_delta > 0,
  }


def correlate(samples):
  gaps = [v for v in samples if v.get('hud_gap_observed_in_interval') is True]
  return {'gap_samples': len(gaps),
          'gap_with_high_cpu': sum(type(v.get('cpu_busy_pct')) in (int, float) and
                                   v['cpu_busy_pct'] >= 85 for v in gaps),
          'gap_with_scheduler_wait': sum(type(v.get('max_runqueue_wait_ms')) in (int, float) and
                                          v['max_runqueue_wait_ms'] >= 50 for v in gaps),
          'causation_proven': False,
          'note': '동일 젯슨 표본의 동시 발생일 뿐, 원인 또는 실제 AI 추론을 증명하지 않습니다.'}


class PerformanceHistoryMonitor:
  """Single writer with a nonblocking process lock. Failure never blocks driving."""
  def __init__(self):
    self.stop = threading.Event()
    self.thread = threading.Thread(target=self.run, name='nexo-perf-history', daemon=True)
    self.lock = None
    self.active = False

  def __enter__(self):
    try:
      RUNTIME.mkdir(parents=True, exist_ok=True)
      self.lock = LOCK.open('a+b')
      fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
      self.active = True
      self.thread.start()
    except (OSError, RuntimeError):
      if self.lock:
        self.lock.close()
      self.lock = None
    return self

  def __exit__(self, *_args):
    self.stop.set()
    if self.active:
      self.thread.join(timeout=1)
    if self.lock:
      self.lock.close()

  def run(self):
    from openpilot.tools.jetson import manage
    samples = []
    self.stop.wait(2)
    while not self.stop.is_set():
      started = time.monotonic()
      try:
        units = manage.services()
        sample = manage.performance_sample(units, manage.root_for(units), interval=.25)
        item = compact(sample, samples[-1] if samples else None)
        samples = (samples + [item])[-MAX_SAMPLES:]
        atomic_json(HISTORY, {'schema_version': 1, 'read_only': True,
                              'updated': time.monotonic(), 'interval_target_s': INTERVAL_S,
                              'samples': samples, 'correlation': correlate(samples)})
      except Exception:  # Hardware/status tools must never terminate the passive observer.
        pass
      self.stop.wait(max(1., INTERVAL_S - (time.monotonic() - started)))
