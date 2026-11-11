"""Read-only AI/HUD unified diagnostic tests; no vehicle or Jetson device needed."""
import json
from pathlib import Path

import pytest

from openpilot.selfdrive.carrot.server.services import jetson_performance as perf


def test_comma_does_not_claim_inference_from_enabled_marker_or_ready_only(monkeypatch, tmp_path):
  now = 1000.0
  enabled = tmp_path / 'enabled'
  enabled.touch()
  model = tmp_path / 'model.json'
  link = tmp_path / 'link.json'
  model.write_text(json.dumps({
    'updated': now-.1, 'ready': True, 'active': False,
    'model': perf.MODEL_NAME, 'last_inference_at_mono': now-.5,
    'modeld_roundtrip_ms': 21.6, 'jetson_execution_us': 14000,
    'completed_inferences': 24,
  }))
  link.write_text(json.dumps({'updated': now-.1, 'state': 'ready'}))
  monkeypatch.setattr(perf, 'MODEL_STATUS', model)
  monkeypatch.setattr(perf, 'LINK_STATUS', link)
  monkeypatch.setattr(perf, 'ENABLE', enabled)
  result = perf.collect_comma(now)
  assert result['external_inference_proven'] is False
  assert result['inference_state'] == 'not_active'
  assert result['timing_is_historical'] is True
  assert result['timing']['modeld_roundtrip_ms'] == 21.6
  value = json.loads(model.read_text())
  value['active'] = True
  model.write_text(json.dumps(value))
  live = perf.collect_comma(now)
  assert live['external_inference_proven'] is True and live['inference_state'] == 'active'
  enabled.unlink()
  assert perf.collect_comma(now)['inference_state'] == 'disabled'


def test_comma_rejects_stale_future_and_corrupt_model_status(monkeypatch, tmp_path):
  model = tmp_path / 'model.json'
  link = tmp_path / 'link.json'
  enabled = tmp_path / 'enabled'
  enabled.touch()
  link.write_text('{}')
  monkeypatch.setattr(perf, 'MODEL_STATUS', model)
  monkeypatch.setattr(perf, 'LINK_STATUS', link)
  monkeypatch.setattr(perf, 'ENABLE', enabled)
  for updated in (0., 102., None):
    model.write_text(json.dumps({'updated': updated, 'active': True, 'ready': True,
                                 'model': perf.MODEL_NAME, 'modeld_roundtrip_ms': 5}))
    result = perf.collect_comma(now=100)
    assert not result['external_inference_proven']
    assert result['timing'] == {}
    assert result['inference_state'] == 'unknown_stale_model'
  model.write_text('not json')
  assert perf.collect_comma(100)['model_status_fresh'] is False


def test_summary_never_invents_gpu_gc_engine_or_physical_panel_success():
  comma = {'inference_state': 'not_active', 'external_inference_proven': False, 'timing': {}}
  missing = perf.summarize(comma, None, {'installed_model': {'matching_engine_files': []}})
  assert not missing['jetson_performance_collected']
  assert any('TensorRT' in note for note in missing['warnings'])
  assert 'jetson_gpu' not in missing['evidence']
  remote = {'ok': True, 'performance': {'clocks': {'gpu_available': False},
    'scheduler_observation_available': False,
    'hud_python_gc': {'available': False}, 'hud_pipeline': {'panel_gap_count': 12},
    'memory': {'memavailable_kb': 12}, 'processes': [], 'server_scheduler_threads': [],
  }}
  result = perf.summarize(comma, remote)
  assert result['jetson_performance_collected']
  assert any('GC' in note for note in result['warnings'])
  assert result['evidence']['hud_pipeline']['panel_gap_count'] == 12
  assert '물리' in ' '.join(result['limitations'])
  assert 'AI 추론' in perf.format_text(result)
