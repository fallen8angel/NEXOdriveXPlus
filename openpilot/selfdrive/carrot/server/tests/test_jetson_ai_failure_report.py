"""AI unified diagnosis: distinguishes stale evidence from verified GPU work."""
import copy

from openpilot.selfdrive.carrot.server.services.jetson_ai_failure_report import evaluate, format_text


SHA = 'f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5'
OLD = '09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec'


def readiness(*, sha=SHA, errors=None, verified=False, engine=False, installed='display_only_installed'):
  return {'ok': True, 'inference_readiness': {
    'installed_sha256': sha, 'install_state': installed, 'display_only_installed': installed != 'installed',
    'engine_file_present': engine, 'synthetic_inference_verified': verified,
    'package_model_matches': True, 'ncm_model_matches': True,
    'engine_build_errors': errors or [], 'blockers': ['CURRENT_TRT_ENGINE_NOT_BUILT']}}


def model(sha=SHA):
  return {'ok': True, 'installed_model': {'onnx_sha256': sha, 'onnx_bytes': 58_000_000,
    'package_contract': {'sha256': sha}, 'package_runtime': {'l4t': '36.4.7', 'tensorrt': '10.3.0'},
    'builder_fingerprint': {'available': True, 'sha256': OLD}}}


def diagnose(remote=None, installed=None, experiments=None, *, sha=SHA):
  return evaluate({'onroad': True, 'offroad': False}, {'external_inference_proven': False},
                  remote, installed, experiments, comma_model_sha=sha)


def test_detects_current_conv_gelu_failure_without_claiming_root_cause():
  details = [{'file': SHA[:16] + '.trt10.3.0.Orin-sm87.build.json',
              'failure_diagnosis': {'category': 'conv_gelu_tactic_unavailable', 'root_cause_proven': False}}]
  report = diagnose(readiness(errors=details), model())
  assert report['stage_code'] == 'engine_build_failed'
  assert report['root_cause_category'] == 'conv_gelu_tactic_unavailable'
  assert report['root_cause_proven'] is False
  assert report['model']['comma_jetson_sha_match'] is True
  assert report['runtime']['builder_fingerprint']['sha256'] == OLD
  assert report['engine']['current_model_failure_count'] == 1
  assert report['engine_build_performed'] is False
  assert report['activation_performed'] is False
  assert 'Conv2D + GELU' in format_text(report)


def test_old_engine_build_failure_is_not_current_model_failure():
  old = [{'file': OLD[:16] + '.trt10.3.0.Orin-sm87.build.json',
          'failure_diagnosis': {'category': 'conv_gelu_tactic_unavailable'}}]
  report = diagnose(readiness(errors=old), model())
  assert report['root_cause_category'] == 'not_established'
  assert report['stage_code'] == 'engine_missing'
  assert report['engine']['current_model_failure_count'] == 0


def test_no_ssh_is_honestly_unavailable_and_text_remains_informative():
  report = diagnose()
  assert report['stage_code'] == 'remote_unavailable'
  assert report['model']['comma_jetson_sha_match'] is None
  assert report['experiments']['collected'] is False
  assert report['engine']['gpu_synthetic_verified'] is None
  assert 'SSH' in format_text(report)
  assert not report['real_vehicle_inference_verified_by_report']


def test_model_mismatch_takes_precedence_over_build_error():
  report = diagnose(readiness(), model(), sha=OLD)
  assert report['stage_code'] == 'model_mismatch'
  assert report['model']['comma_jetson_sha_match'] is False


def test_experiment_from_another_model_never_counts_as_current():
  experiment = {'ok': True, 'experiments': {
    'model_sha256': OLD, 'state': 'experiment_complete', 'native_gpu_verified': True,
    'trials': [{'name': 'native_fp16', 'state': 'passed', 'synthetic_fps': 999.0}]}}
  report = diagnose(readiness(), model(), experiment)
  assert report['experiments']['collected'] is True
  assert report['experiments']['current_model_results'] is False
  assert report['experiments']['trials'] == []
  assert report['stage_code'] == 'engine_missing'


def test_current_experiment_failure_is_classified_without_activation():
  experiment = {'ok': True, 'experiments': {
    'model_sha256': SHA, 'state': 'failed',
    'trials': [{'name': 'native_fp16', 'state': 'failed'}],
    'failed_build_excerpts': [{'trial': 'native_fp16', 'failure_diagnosis': {
      'category': 'tactic_unavailable'}}]}}
  report = diagnose(readiness(), model(), experiment)
  assert report['experiments']['trials'][0]['failure_category'] == 'tactic_unavailable'
  assert report['root_cause_category'] == 'tactic_unavailable'
  assert not report['engine_build_performed']


def test_trial_failure_phase_is_explained_without_claiming_gpu_success():
  experiment = {'ok': True, 'experiments': {'model_sha256': SHA, 'state': 'passed',
    'trials': [{'name': 'native_fp16', 'state': 'failed', 'failed_phase': 'building',
                'verbose_log_path': '/opt/diagnostic/verbose.log',
                'failure_diagnosis': {'category': 'conv_gelu_tactic_unavailable'}}]}}
  report = diagnose(readiness(), model(), experiment)
  row = report['experiments']['trials'][0]
  assert row['failed_phase'] == 'building'
  assert row['verbose_log_available'] is True
  assert '실패 단계 building' in format_text(report)
  assert 'VERBOSE 로그 존재' in format_text(report)
  assert 'native_no_fp16_flag' in report['experiments']['proposed_comparison']
  assert not report['engine_build_performed'] and not report['activation_performed']


def test_verified_synthetic_does_not_imply_vehicle_inference():
  report = diagnose(readiness(verified=True, engine=True, installed='installed'), model())
  assert report['stage_code'] == 'vehicle_unverified'
  assert report['engine']['gpu_synthetic_verified'] is True
  assert report['real_vehicle_inference_verified_by_report'] is False


def test_no_mutations_and_no_raw_diagnostic_secrets_in_txt():
  remote = readiness()
  original = copy.deepcopy(remote)
  report = diagnose(remote, model())
  assert remote == original
  text = format_text(report)
  assert 'password' not in text.lower()
  assert 'private_key' not in text.lower()


def test_real_bundle_reverse_route_and_missing_key_are_separate_blockers():
  report = evaluate(
    {'onroad': True, 'offroad': False}, {'external_inference_proven': False},
    readiness(), model(), None,
    remote_ssh={'reverse_result_fresh': False, 'jetson_to_comma': {
      'ok': False, 'code': 'USB_NCM_UNAVAILABLE', 'ncm_reason': 'ROUTE_NOT_DIRECT',
      'route_observed': {'dev': 'wlan0', 'src': '192.168.100.250'},
      'client': {'identity_files': [], 'known_hosts': [], 'agent_available': False}}},
    comma_model_sha=SHA,
    lte_gateway={'configured': False, 'operational': False,
                 'failures': [{'code': 'ACTIVE_CELLULAR_WAN_UNVERIFIED'}],
                 'uplink_diagnostics': {'default_interface': 'wlan0'}})
  assert report['connectivity']['reverse_ncm_reason'] == 'ROUTE_NOT_DIRECT'
  assert report['connectivity']['reverse_identity_available'] is False
  assert report['connectivity']['reverse_ssh_result_fresh'] is False
  assert report['lte_gateway']['default_interface'] == 'wlan0'
  assert report['lte_gateway']['failure_codes'] == ['ACTIVE_CELLULAR_WAN_UNVERIFIED']
  assert 'SSH 키' in format_text(report) and 'LTE 공유' in format_text(report)
  assert report['read_only'] and not report['activation_performed']
