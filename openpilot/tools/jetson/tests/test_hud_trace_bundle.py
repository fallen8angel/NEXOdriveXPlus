"""Read-only HUD trace used by the one-click Jetson diagnostic bundle."""
import json

from openpilot.tools.jetson import manage


def test_hud_trace_reads_metadata_and_age(tmp_path):
  path = tmp_path / 'hud-pipeline-trace.json'
  path.write_text(json.dumps({'version': 1, 'updated': 100.0, 'input_mode': 'vehicle',
                              'panel_gap_count': 3, 'panel_gap_max_s': .31}))
  original = manage.time.monotonic
  try:
    manage.time.monotonic = lambda: 100.2
    value = manage.read_hud_trace(path)
  finally:
    manage.time.monotonic = original
  assert value['ok'] is True
  assert value['hud_pipeline_trace']['panel_gap_count'] == 3
  assert value['trace_age_s'] == .2


def test_hud_trace_rejects_symlink_and_oversized_content(tmp_path):
  source = tmp_path / 'source.json'
  source.write_text('{}')
  shortcut = tmp_path / 'shortcut.json'
  shortcut.symlink_to(source)
  assert manage.read_hud_trace(shortcut)['ok'] is False
  huge = tmp_path / 'huge.json'
  huge.write_text('x' * 65537)
  assert manage.read_hud_trace(huge)['ok'] is False


def test_hud_trace_missing_file_is_not_healthy(tmp_path):
  assert manage.read_hud_trace(tmp_path / 'missing.json')['ok'] is False
