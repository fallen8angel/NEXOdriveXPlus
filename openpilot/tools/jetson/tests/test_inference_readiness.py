"""The AI transition diagnostic never changes USB or model ownership."""
import json
from openpilot.tools.jetson import manage
from openpilot.selfdrive.carrot.server.services.jetson_ai_readiness import evaluate


def test_display_only_old_cache_and_interface_auto_recovery(tmp_path, monkeypatch):
  root = tmp_path / 'runtime'
  (root / 'updates').mkdir(parents=True)
  (root / 'cache').mkdir()
  sha = 'f' * 64
  (root / 'updates/nexo-install-status.json').write_text(json.dumps({
    'state': 'display_only_installed', 'display_only': True}))
  (root / 'updates/nexo-ncm.json').write_text(json.dumps({
    'interface': 'previous0', 'model_sha256': sha, 'display_only': True}))
  (root / 'cache/last-loaded.json').write_text(json.dumps({'sha256': '0' * 64}))
  monkeypatch.setattr(manage, 'installed_model', lambda root: {
    'onnx_sha256': sha, 'package_contract': {'sha256': sha}, 'matching_engine_files': []})
  def record(path):
    return ({'state': 'ready', 'interface': 'current0', 'updated': 100}
            if str(path) == '/run/nexo-ncm-status.json' else
            json.loads(path.read_text()) if path.exists() else {})
  monkeypatch.setattr(manage, 'record', record)
  monkeypatch.setattr(manage.time, 'monotonic', lambda: 110)
  report = manage.inference_readiness(root)['inference_readiness']
  assert report['display_only_installed']
  assert report['old_cache_expected_in_hud_only']
  assert report['ncm_interface_auto_recovered']
  assert report['ncm_fresh_ready']
  assert 'HUD_ONLY_RUNTIME' in report['blockers']
  assert 'CURRENT_TRT_ENGINE_NOT_BUILT' in report['blockers']
  assert not report['activation_performed'] and not report['engine_build_performed']


def test_onroad_and_unreachable_jetson_are_fail_closed():
  report = evaluate({'onroad': True, 'offroad': False, 'enabled': False}, None)
  assert 'COMMA_OFFROAD_NOT_VERIFIED' in report['blockers']
  assert 'JETSON_READINESS_UNAVAILABLE' in report['blockers']
  assert report['can_activate_now'] is False and report['auto_install_or_build'] is False


def test_package_sha_mismatch_never_allows_activation():
  peer = {'ok': True, 'inference_readiness': {
    'blockers': 'unexpected string', 'package_model_matches': False,
    'ncm_model_matches': False, 'engine_file_present': False,
    'display_only_installed': True}}
  report = evaluate({'onroad': False, 'offroad': True, 'enabled': False}, peer)
  assert 'PACKAGE_MODEL_SHA_MISMATCH' in report['blockers']
  assert 'NCM_MODEL_SHA_MISMATCH' in report['blockers']
  assert 'CURRENT_TRT_ENGINE_NOT_BUILT' in report['blockers']
  assert report['model_hashes_consistent'] is False
  assert not report['can_activate_now'] and not report['auto_install_or_build']
