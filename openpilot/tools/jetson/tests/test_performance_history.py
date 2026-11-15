"""Synthetic-only Jetson history tests: no live sensors or car controls."""
from openpilot.tools.jetson.performance_history import MAX_SAMPLES, compact, correlate


def test_hud_gap_and_cpu_overlap_is_not_causation():
  source = {'performance': {'sample_monotonic': 10., 'cpu_busy_pct': {'cpu': 90},
                           'hud_pipeline': {'panel_gap_count': 2},
                           'server_scheduler_threads': [{'runqueue_wait_ms': 100}]}}
  a = compact(source, unix_now=1000)
  assert a['panel_gap_delta'] is None
  source['performance']['hud_pipeline']['panel_gap_count'] = 3
  b = compact(source, a, unix_now=1005)
  assert b['panel_gap_delta'] == 1
  assert correlate([a, b])['gap_with_high_cpu'] == 1
  assert correlate([a, b])['gap_with_scheduler_wait'] == 1
  assert correlate([a, b])['causation_proven'] is False
  assert MAX_SAMPLES <= 48


def test_hud_gap_counter_reset_is_unknown():
  sample = {'performance': {'hud_pipeline': {'panel_gap_count': 1}}}
  assert compact(sample, {'panel_gap_count': 10})['panel_gap_delta'] is None


def test_cached_management_poll_does_not_spawn_service_queries(monkeypatch):
  from pathlib import Path
  from openpilot.tools.jetson import manage
  real_is_file = Path.is_file
  monkeypatch.setattr(Path, 'is_file', lambda p: str(p) == '/etc/nv_tegra_release' or real_is_file(p))
  monkeypatch.setattr(manage, 'services', lambda: (_ for _ in ()).throw(AssertionError('systemctl in cached poll')))
  monkeypatch.setattr(manage, 'read_performance_history', lambda: {'ok': True, 'history': {'available': False}})
  assert manage.handle('performance_history') == {'ok': True, 'history': {'available': False}}
