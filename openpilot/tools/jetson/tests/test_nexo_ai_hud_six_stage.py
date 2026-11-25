"""Safety tests for six-stage diagnostic acceptance. No physical HUD needed."""
from openpilot.tools.jetson.nexo_ai_hud_six_stage import evaluate, format_text


def test_empty_records_never_pass():
  receipt = evaluate({}, {}, {}, {})
  assert receipt['verified_steps'] == 0
  assert receipt['physical_hud_accepted'] is False
  assert not any(stage['passed'] for stage in receipt['steps'])


def test_update_link_standard_model_only():
  update = {'stage': 'completed', 'installed_matches_target': True}
  comma = {'connected': True, 'display_hello_verified': True, 'hud_tx_recent': True,
           'hud_pipeline': {'panel_write_recent': True, 'renderer_running': True}}
  readiness = {'jetson': {'model_file_ready': True, 'package_model_matches': True,
                         'ncm_model_matches': True}}
  n16 = {'trial': {'state': 'failed', 'phase': 'preflight',
                   'error': 'N16 trial memory reserve below 2 GiB'},
         'hud_observer': {'physical_hud_feed_enabled': True}}
  receipt = evaluate(update, comma, readiness, n16, {'physical_hud_verified': True})
  assert [x['passed'] for x in receipt['steps']] == [True, True, True, False, False, False]
  assert receipt['verified_steps'] == 3
  assert 'memory reserve' in format_text(receipt)
  assert receipt['observations']['previous_physical_hud_flag'] is True
  assert receipt['physical_hud_accepted'] is False


def test_synthetic_n16_success_still_cannot_claim_live_hud():
  from openpilot.tools.jetson.nexo_n16_registry import TRUSTED_SHA256
  n16 = {'trial': {'state': 'passed', 'gpu_inference_verified': True, 'model_sha256': TRUSTED_SHA256},
         'hud_observer': {'physical_hud_feed_enabled': True,
                          'renderable_objects': [{'car': 'fake'}]}}
  receipt = evaluate({}, {}, {}, n16)
  assert receipt['steps'][3]['passed'] is True
  assert receipt['steps'][4]['passed'] is False
  assert receipt['steps'][5]['passed'] is False
  assert receipt['driving_control_enabled_by_report'] is False


def test_strict_bools_no_truthiness_bypass():
  receipt = evaluate({'stage': 'completed', 'installed_matches_target': 1},
                     {'connected': 1, 'display_hello_verified': 1, 'hud_tx_recent': 1},
                     {'jetson': {'model_file_ready': 1, 'package_model_matches': 1,
                                 'ncm_model_matches': 1}},
                     {'trial': {'state': 'passed', 'gpu_inference_verified': 1}})
  assert receipt['verified_steps'] == 0


def test_matching_model_and_complete_split_replay_required_for_execution():
  from openpilot.tools.jetson.nexo_ai_hud_six_stage import n16_execution_verified
  from openpilot.tools.jetson.nexo_n16_registry import TRUSTED_SHA256
  single = {'model_sha256': TRUSTED_SHA256, 'gpu_inference_verified': True, 'state': 'passed'}
  split = {'model_sha256': TRUSTED_SHA256, 'gpu_inference_verified': True, 'state': 'gpu_parity_failed',
           'cut_node_index': 360, 'phase': 'offline_gpu_replay_complete', 'frames_completed': 140}
  assert n16_execution_verified({}, split)
  assert n16_execution_verified(single, {})
  assert not n16_execution_verified({**single, 'model_sha256': 'other'}, {})
  for change in ({'state': 'running'}, {'frames_completed': 139}, {'model_sha256': 'other'}, {'gpu_inference_verified': 1}):
    assert not n16_execution_verified(single, {**split, **change})
