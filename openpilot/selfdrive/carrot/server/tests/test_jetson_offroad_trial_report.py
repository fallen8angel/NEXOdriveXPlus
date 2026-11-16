"""Offroad engine-trial ZIP reports are passive and cannot turn historical evidence into approval."""
from openpilot.selfdrive.carrot.server.services.jetson_offroad_trial_report import evaluate, format_text


def test_gated_full_offroad_snapshot_and_matching_gpu_trial_are_separate_evidence():
  sha = 'a' * 64
  report = evaluate(
    {'offroad': True, 'onroad': False, 'enabled': False}, True,
    {'ok': True, 'trial': 'native_fp16', 'time': 123},
    {'ok': True, 'experiments': {'available': True, 'state': 'passed', 'model_sha256': sha,
                                'native_gpu_verified': True,
                                'trials': [{'name': 'native_fp16', 'state': 'passed', 'synthetic_fps': 22.0}]}}, sha)
  assert report['safety']['eligible_local_gate_at_collection'] is True
  assert report['launch']['accepted'] is True
  assert report['experiment']['native_gpu_verified_for_matching_model'] is True
  assert report['bundle_collection_read_only'] and not report['trial_started_by_bundle']
  assert report['vehicle_inference_verified'] is False and not report['production_ai_activated_by_bundle']
  assert '실제 차량 AI 추론 검증: 아니요' in format_text(report)


def test_incomplete_offroad_or_external_ai_enabled_blocks_local_gate_even_with_old_start():
  for local, live in [
    ({'offroad': True, 'onroad': False, 'enabled': False}, False),
    ({'offroad': False, 'onroad': True, 'enabled': False}, True),
    ({'offroad': True, 'onroad': False, 'enabled': True}, True),
    ({'offroad': None, 'onroad': False, 'enabled': False}, True),
  ]:
    report = evaluate(local, live, {'ok': True, 'trial': 'native_fp16'}, None, 'a' * 64)
    assert report['safety']['eligible_local_gate_at_collection'] is False
    assert report['launch']['recorded'] is True
    assert report['experiment']['native_gpu_verified_for_matching_model'] is None
    assert report['trial_started_by_bundle'] is False


def test_stale_model_gpu_success_is_not_current_model_proof():
  report = evaluate(
    {'offroad': True, 'onroad': False, 'enabled': False}, True,
    remote_experiments={'ok': True, 'experiments': {
      'available': True, 'state': 'passed', 'model_sha256': 'b' * 64,
      'native_gpu_verified': True, 'trials': [{'name': 'native_fp16', 'state': 'passed'}]}},
    comma_sha='a' * 64)
  assert report['experiment']['matches_current_comma_model'] is False
  assert report['experiment']['native_gpu_verified_for_matching_model'] is False
  assert not report['vehicle_inference_verified']


def test_unreachable_jetson_never_invents_trial_results():
  report = evaluate({'offroad': True, 'onroad': False, 'enabled': False}, True, None, None, None)
  assert report['experiment']['available'] is False
  assert report['experiment']['matches_current_comma_model'] is None
  assert report['experiment']['trials'] == []
  assert report['experiment']['native_gpu_verified_for_matching_model'] is None
  assert '미검증' in format_text(report)


def test_failure_category_and_phase_are_exported_without_promoting_to_gpu_success():
  sha = 'c' * 64
  report = evaluate(
    {'offroad': True, 'onroad': False, 'enabled': False}, True,
    {'ok': False, 'error': 'SSH_HOST_KEY_UNKNOWN', 'trial': 'native_fp16'},
    {'ok': True, 'experiments': {
      'available': True, 'state': 'failed', 'model_sha256': sha,
      'trials': [{'name': 'native_fp16', 'state': 'failed', 'failed_phase': 'build',
                  'failure_diagnosis': {'category': 'conv_gelu_tactic_unavailable'}}]}}, sha)
  assert report['launch']['accepted'] is False
  assert report['experiment']['trials'][0]['failure_category'] == 'conv_gelu_tactic_unavailable'
  assert 'SSH_HOST_KEY_UNKNOWN' in format_text(report)
  assert 'conv_gelu_tactic_unavailable' in format_text(report)
  assert report['experiment']['native_gpu_verified_for_matching_model'] is None
