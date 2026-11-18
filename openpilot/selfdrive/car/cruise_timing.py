"""Bounded read-only cruise/card latency observations for NEXO.

Measurements are wall time (including waits) versus thread CPU time.
This module never changes control outputs, CAN, params, or network state.
Nothing is logged per frame: the caller receives at most one rollup per
30 seconds. Correlated peaks retain their own frame and receive timestamp.
"""
import math

WINDOW_NS = 30_000_000_000
METRICS = (
  'card_total_ms', 'card_total_cpu_ms', 'card_tail_after_receive_ms', 'card_state_ms', 'card_publish_ms',
  'card_controls_ms', 'cruise_total_ms', 'cruise_total_cpu_ms',
  'cruise_params_ms', 'cruise_params_cpu_ms',
  'cruise_buttons_ms', 'cruise_buttons_cpu_ms',
  'cruise_button_prepare_ms', 'cruise_button_prepare_cpu_ms',
  'cruise_carrot_command_ms', 'cruise_carrot_command_cpu_ms',
  'cruise_brake_gas_ms', 'cruise_brake_gas_cpu_ms',
)


class CruiseTimingWindow:
  """Aggregate scalar timings; no unbounded records or frame-by-frame I/O."""

  def __init__(self, window_ns=WINDOW_NS):
    if window_ns <= 0:
      raise ValueError('window_ns must be positive')
    self.window_ns = window_ns
    self.reset()

  def reset(self):
    self.started_ns = None
    self.sample_count = 0
    self.totals = {}
    self.counts = {}
    self.maxima = {}
    self.slow_card_count = 0
    self.slow_cruise_count = 0
    self.peak_card = None
    self.peak_cruise = None

  @staticmethod
  def _sample(metrics):
    if not isinstance(metrics, dict):
      return {}
    result = {}
    for name in METRICS:
      value = metrics.get(name)
      if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 60_000:
        continue
      result[name] = round(float(value), 3)
    return result

  @staticmethod
  def _peak(sample, *, frame, received_ns, params_refreshed):
    return {
      'cruise_frame': frame if type(frame) is int and frame >= 0 else None,
      'receive_mono_ns': received_ns if type(received_ns) is int and received_ns >= 0 else None,
      'params_refreshed': params_refreshed if type(params_refreshed) is bool else None,
      'stages_ms': sample.copy(),
    }

  def record(self, metrics, *, frame, receive_mono_ns, finished_mono_ns, params_refreshed):
    """Return a 30-second rollup or None. The caller controls the log sink."""
    if type(finished_mono_ns) is not int or finished_mono_ns < 0:
      return None
    sample = self._sample(metrics)
    if not sample:
      return None
    if self.started_ns is None:
      self.started_ns = finished_mono_ns
    if finished_mono_ns < self.started_ns:
      self.reset()
      self.started_ns = finished_mono_ns

    self.sample_count += 1
    for key, value in sample.items():
      self.totals[key] = self.totals.get(key, 0.) + value
      self.counts[key] = self.counts.get(key, 0) + 1
      self.maxima[key] = max(self.maxima.get(key, 0.), value)
    card_time = sample.get('card_tail_after_receive_ms')
    cruise_time = sample.get('cruise_total_ms')
    if card_time is not None:
      self.slow_card_count += card_time > 10.0  # 100 Hz card budget.
      if self.peak_card is None or card_time > self.peak_card['stages_ms'].get('card_tail_after_receive_ms', -1.):
        self.peak_card = self._peak(sample, frame=frame, received_ns=receive_mono_ns, params_refreshed=params_refreshed)
    if cruise_time is not None:
      self.slow_cruise_count += cruise_time > 5.0
      if self.peak_cruise is None or cruise_time > self.peak_cruise['stages_ms'].get('cruise_total_ms', -1.):
        self.peak_cruise = self._peak(sample, frame=frame, received_ns=receive_mono_ns, params_refreshed=params_refreshed)

    elapsed_ns = finished_mono_ns - self.started_ns
    if elapsed_ns < self.window_ns:
      return None
    report = {
      'schema_version': 1,
      'read_only': True,
      'elapsed_ms': round(elapsed_ns / 1e6, 1),
      'samples': self.sample_count,
      'slow_card_tail_over_10ms': self.slow_card_count,
      'slow_cruise_over_5ms': self.slow_cruise_count,
      'avg_ms': {key: round(self.totals[key] / self.counts[key], 3) for key in self.totals},
      'max_ms': self.maxima.copy(),
      'peak_card': self.peak_card,
      'peak_cruise': self.peak_cruise,
    }
    self.reset()
    return report
