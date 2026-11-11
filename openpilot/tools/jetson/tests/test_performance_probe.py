"""Isolated Jetson CPU/GPU/scheduler/GC performance probes: synthetic data only."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import manage
from openpilot.tools.jetson import hud_gc_observer


def test_proc_cpu_delta_discards_invalid_and_decreasing_values():
  assert manage._cpu_usage({'cpu': (100, 20)}, {'cpu': (200, 40)}) == {'cpu': 80.0}
  assert manage._cpu_usage({'cpu': (100, 20)}, {'cpu': (90, 10)}) == {}
  assert manage._cpu_usage({'cpu': (100, 20)}, {'cpu': (200, 10)}) == {}


def test_thread_delta_uses_jetson_local_time_only_and_bounded_output():
  before = {('jetson', 123): (100_000_000, 20_000_000)}
  after = {('jetson', 123): (200_000_000, 35_000_000)}
  sample = manage._thread_usage(before, after, 0.5)
  assert len(sample) == 1
  assert sample[0]['cpu_time_ms'] == pytest.approx(100)
  assert sample[0]['runqueue_wait_ms'] == pytest.approx(15)
  assert manage._thread_usage(before, after, 0) == []
  assert manage._thread_usage(before, {('jetson', 123): (50, 10)}, .5) == []


def test_gc_missing_or_stale_is_unknown_not_false_zero(monkeypatch):
  monkeypatch.setattr(manage, 'record', lambda path: {})
  value = manage._python_gc()
  assert value == {'available': False, 'reason': 'hud_gc_observer_unavailable_or_stale'}


def test_on_demand_performance_has_no_external_side_effects(monkeypatch):
  monkeypatch.setattr(manage, '_main_pids', lambda units: {'jetson': 99})
  counter = [{'cpu': (100, 20)}, {'cpu': (200, 40)}]
  threads = [{('jetson', 123): (100000, 50000)},
             {('jetson', 123): (300000, 90000)}]
  monkeypatch.setattr(manage, '_cpu_counters', lambda: counter.pop(0))
  monkeypatch.setattr(manage, '_thread_counters', lambda pids: threads.pop(0))
  monotonic = [100., 100.5, 100.7]
  monkeypatch.setattr(manage.time, 'monotonic', lambda: monotonic.pop(0))
  monkeypatch.setattr(manage.time, 'sleep', lambda secs: None)
  monkeypatch.setattr(manage, '_proc_memory', lambda pid: {'rss_kb': 12000})
  monkeypatch.setattr(manage, '_python_gc', lambda: {'available': False})
  monkeypatch.setattr(manage, 'read_hud_trace', lambda: {
    'ok': True, 'trace_age_s': .1,
    'hud_pipeline_trace': {'panel_gap_count': 6, 'encoder_expired_count': 3}})
  monkeypatch.setattr(manage, '_gpu_evidence', lambda: {'gpu_available': True, 'gpu': [{'clock_mhz': 625.}]})
  monkeypatch.setattr(manage, '_power_mode', lambda: {'mode': 'MAXN'})
  monkeypatch.setattr(manage, '_meminfo', lambda: {'memavailable_kb': 42})
  monkeypatch.setattr(manage, 'control_state', lambda root: {'engine': {'state': 'ready'}})
  output = manage.performance_sample({'jetson': {'ActiveState': 'active'}}, Path('/tmp/nowhere'))
  p = output['performance']
  assert output['ok'] is True and p['read_only'] is True
  assert p['cpu_busy_pct']['cpu'] == 80.0
  assert p['hud_pipeline']['panel_gap_count'] == 6
  assert p['processes'][0]['rss_kb'] == 12000
  assert p['server_scheduler_threads'][0]['runqueue_wait_ms'] > 0
  assert not p['hud_python_gc']['available']


def test_hud_gc_observer_records_only_actual_events_without_forcing_gc(tmp_path):
  clock = [100.]
  observed = hud_gc_observer.Observer(tmp_path / 'gc.json', clock=lambda: clock[0])
  observed.callback('start', {'generation': 0})
  clock[0] += .012
  observed.callback('stop', {'generation': 0})
  observed.callback('stop', {'generation': -1})
  clock[0] += 5.
  result = observed.snapshot()
  assert result['monitoring_only']
  assert result['gc_pause_total_ms'] == pytest.approx(12)
  assert result['recent_max_pause_ms'] == pytest.approx(12)
  assert result['generations'][0]['observed_collections'] == 1
  observed.publish()
  saved = json.loads((tmp_path / 'gc.json').read_text())
  assert saved['version'] == 1
  assert saved['generations'][0]['observed_collections'] == 0
