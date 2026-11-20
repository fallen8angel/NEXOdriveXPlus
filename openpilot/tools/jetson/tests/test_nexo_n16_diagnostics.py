"""Pure-Python tests for N16 shared-memory and HUD fail-closed integration."""
import json
from pathlib import Path

import pytest

from openpilot.tools.jetson.nexo_n16_diagnostics import (
  Timeline, assess_feasibility, classify_trial, hud_observer_contract, meminfo_bytes,
  proc_memory_bytes, read_report, save_json, summary,
)
from openpilot.tools.jetson.nexo_n16_hud_observer import summarize_raw_model_output


def test_meminfo_is_bytes_and_missing_fields_stay_unknown(tmp_path):
  p = tmp_path / 'meminfo'
  p.write_text('MemTotal: 7802712 kB\nMemAvailable: 6622072 kB\n'
               'CmaFree: 206008 kB\nSwapFree: 22 kB\n')
  value = meminfo_bytes(p)
  assert value['MemAvailable_bytes'] == 6622072 * 1024
  assert value['CmaFree_bytes'] == 206008 * 1024
  assert 'cuda_peak_bytes' not in value


def test_process_tree_is_bounded_and_read_only(tmp_path):
  p = tmp_path / 'proc'
  for pid, children, rss in ((123, '124', 10), (124, '', 20)):
    directory = p / str(pid) / 'task' / str(pid)
    directory.mkdir(parents=True)
    (directory / 'children').write_text(children)
    (p / str(pid) / 'status').write_text('VmRSS:\t%d kB\nVmHWM:\t%d kB\nVmSwap:\t0 kB\n' % (rss, rss))
  got = proc_memory_bytes(123, p)
  assert got['worker_processes_observed'] == 2
  assert got['worker_rss_bytes'] == 30 * 1024
  assert got['worker_rss_highwater_sum_bytes'] == 30 * 1024


def test_timeline_bounded_and_low_water_mark():
  value = Timeline(limit=3)
  for i in range(7):
    value.record({'MemAvailable_bytes': 9_000 - i * 1000, 'worker_rss_bytes': i * 200}, 'tensorrt_build')
  result = value.export()
  assert result['samples_taken'] == 7
  assert len(result['samples']) == 3
  assert result['minimum_memavailable_bytes'] == 3000
  assert result['maximum_worker_rss_bytes'] == 1200
  assert result['cuda_memory_peak_verified'] is False
  with pytest.raises(ValueError):
    Timeline(limit=9999)


@pytest.mark.parametrize('trial,expected', [
  ({}, 'no_verified_gpu_inference'),
  ({'state': 'failed', 'error': 'N16 trial memory reserve below 2 GiB'}, 'host_memory_guard'),
  ({'state': 'failed', 'error': 'Cuda Runtime (out of memory)'}, 'cuda_allocation_failed'),
  ({'state': 'failed', 'error': 'build returned no engine'}, 'engine_build_or_validation_failed'),
  ({'state': 'passed', 'gpu_inference_verified': True}, 'synthetic_only'),
])
def test_trial_diagnosis_cannot_approve_production(trial, expected):
  result = classify_trial(trial)
  assert result['diagnosis_code'] == expected
  assert result['production_activation_allowed'] is False
  assert result['repeat_same_build_recommended'] is False


def test_hud_observer_never_renders_even_for_synthetic_gpu_success():
  values = hud_observer_contract({'state': 'passed', 'gpu_inference_verified': True},
                                 numerical_accuracy_verified=True,
                                 camera_geometry_verified=True,
                                 live_camera_verified=True)
  assert values['renderable_objects'] == []
  assert values['physical_hud_feed_enabled'] is False
  assert values['vehicle_control_enabled'] is False
  assert values['traffic_lights_available_from_n16_contract'] is False
  incomplete = hud_observer_contract({'state': 'failed'})
  assert len(incomplete['blockers']) == 4


def test_read_only_status_files_include_offline_hud_and_timeline(tmp_path):
  result = tmp_path / 'result.json'
  timeline = tmp_path / 'memory.json'
  save_json(result, {'state': 'failed', 'phase': 'tensorrt_build'})
  save_json(timeline, {'samples': [{'MemAvailable_bytes': 123}]})
  out = summary(result, timeline)
  assert out['diagnosis']['production_activation_allowed'] is False
  assert out['hud_observer']['physical_hud_feed_enabled'] is False
  assert len(out['memory_timeline']['samples']) == 1
  assert read_report(result)['state'] == 'failed'


def test_refuse_symlinked_status_destination(tmp_path):
  target = tmp_path / 'target'
  target.write_text('unmodified')
  link = tmp_path / 'status'
  link.symlink_to(target)
  with pytest.raises(ValueError):
    save_json(link, {'change': True})
  assert target.read_text() == 'unmodified'


def test_offline_raw_hud_vectors_are_never_projected():
  output = summarize_raw_model_output([0.0] * 18580, source='synthetic')
  assert output['external_hud_render_allowed'] is False
  assert output['controls_allowed'] is False
  assert output['renderable_objects'] == []
  assert output['traffic_lights_detected'] is None
  assert set(output['raw_output_sections']) == {
    'lane_lines', 'road_edges', 'lead', 'lead_prob', 'plan'
  }
  assert all(x['all_values_finite'] for x in output['raw_output_sections'].values())
  with pytest.raises(ValueError):
    summarize_raw_model_output([0.0] * 18)
  with pytest.raises(ValueError):
    summarize_raw_model_output([0.0] * 18580, source='onroad_live')


def test_nonfinite_raw_values_are_not_inferred_as_vehicles():
  output = [0.0] * 18580
  output[1907] = float('nan')
  result = summarize_raw_model_output(output)
  assert result['raw_output_sections']['lead']['all_values_finite'] is False
  assert result['renderable_objects'] == []


def test_n16_feasibility_reports_observed_headroom_without_claiming_cuda_peak():
  receipt = assess_feasibility(
    {'state': 'failed', 'phase': 'tensorrt_build',
     'error': 'N16 trial memory reserve below 2 GiB',
     'available_memory': 6763393024},
    {'minimum_memavailable_bytes': 2115321856})
  assert receipt['observed_available_memory_drop_bytes'] == 4648071168
  assert receipt['peak_cuda_allocation_bytes_measured'] is None
  assert receipt['engine_created_and_loaded'] is False
  assert receipt['production_vehicle_approved'] is False


def test_7000_n16_management_status_cannot_activate_hud(tmp_path):
  from openpilot.tools.jetson.manage import n16_trial_status
  cache = tmp_path / 'cache'
  cache.mkdir()
  (cache / 'nexo-n16-trial-result.json').write_text(json.dumps({
    'state': 'passed', 'phase': 'synthetic_inference',
    'gpu_inference_verified': True, 'split_builder': True,
    'hud_observer': {'physical_hud_feed_enabled': True},
  }))
  (cache / 'nexo-n16-trial-memory.json').write_text(json.dumps({
    'samples_taken': 3, 'minimum_memavailable_bytes': 2400000000,
    'samples': [{'MemAvailable_bytes': 2400000000}],
  }))
  status = n16_trial_status(tmp_path)
  assert status['ok'] is True and status['read_only'] is True
  assert status['trial']['gpu_inference_verified'] is True
  assert status['hud_observer']['physical_hud_feed_enabled'] is False
  assert status['hud_observer']['renderable_objects'] == []
  assert status['diagnosis']['production_activation_allowed'] is False
  assert len(status['memory']['recent_samples']) == 1
