"""The AI transition diagnostic never changes USB or model ownership."""
import json
import hashlib
from pathlib import Path
from openpilot.tools.jetson import manage
from openpilot.selfdrive.carrot.server.services.jetson_ai_readiness import evaluate


def test_readiness_reads_only_plans_with_the_current_model_cache_key(tmp_path, monkeypatch):
  model = tmp_path / 'current/openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
  model.parent.mkdir(parents=True)
  model.write_bytes(b'current model')
  sha = hashlib.sha256(model.read_bytes()).hexdigest()
  old_sha = 'a' * 64
  engines = tmp_path / 'cache/engines'
  engines.mkdir(parents=True)
  def plan(key, model_sha):
    path = engines / (key + '.plan')
    path.write_bytes(key.encode())
    path.with_suffix('.json').write_text(json.dumps({'spec': {'sha256': model_sha}, 'trt_version': '10.3.0',
                                                     'device': 'Orin-sm87'}))
    return path
  suffix = '.trt10.3.0.Orin-sm87'
  current = plan(sha[:16] + suffix, sha)
  old = plan(old_sha[:16] + suffix, old_sha)
  renamed = plan('b' * 16 + suffix, sha)
  wrong_tag = plan(sha[:16] + '.trt10.3.0.Other-sm87', sha)
  monkeypatch.setattr(manage, 'run', lambda args: {'output': 'test runtime'})
  opened = []
  original_open = Path.open
  def observe(path, *args, **kwargs):
    if path.suffix == '.plan':
      opened.append(path)
    return original_open(path, *args, **kwargs)
  monkeypatch.setattr(Path, 'open', observe)
  ready = manage.inference_readiness(tmp_path)['inference_readiness']
  assert opened == [current] and ready['matching_engine_files'] == [current.stem]
  inventory = {i['file']: i for i in ready['engine_inventory']}
  for path in (old, renamed, wrong_tag):
    assert inventory[path.name]['hash_status'] == 'skipped_unrelated_plan'
    assert inventory[path.name]['plan_sha256'] is None
  assert inventory[current.name]['plan_sha256'] == hashlib.sha256(current.stem.encode()).hexdigest()
  assert not inventory[renamed.name]['cache_key_matches_metadata'] and not inventory[wrong_tag.name]['cache_key_matches_metadata']
  opened.clear()
  audit = manage.installed_model(tmp_path)
  assert set(opened) == {old, current, renamed, wrong_tag}
  assert all(item['plan_sha256'] for item in audit['engine_inventory'])

  report = tmp_path / 'cache/nexo-engine-experiments.json'
  for tested_sha, state, expected in ((old_sha, 'experiment_complete', False), (sha, 'failed', False),
                                      (sha, 'experiment_complete', True)):
    report.write_text(json.dumps({'model_sha256': tested_sha, 'state': state, 'native_gpu_verified': True}))
    readiness = manage.inference_readiness(tmp_path)['inference_readiness']
    assert readiness['native_gpu_experiment_verified'] is expected
    assert readiness['vehicle_inference_verified'] is False


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
  monkeypatch.setattr(manage, 'installed_model', lambda root, *, hash_other_plans: {
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
  assert len(report['next_checks']) == len(report['blockers'])
  assert any('Offroad' in text for text in report['next_checks'])


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
