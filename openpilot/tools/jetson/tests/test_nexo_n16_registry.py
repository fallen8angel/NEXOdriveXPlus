"""No N16 activation, NEXO CAN writes or vehicle model replacement in stage 1."""
import hashlib
import json
from pathlib import Path
import pytest

from openpilot.tools.jetson import nexo_n16_registry as registry


def test_pinned_upstream_contract_and_2019_nexo_profile():
  spec, profile = registry.load_registry()
  assert spec['sha256'] == registry.TRUSTED_SHA256
  assert spec['nbytes'] == 814591107
  assert spec['frame_skip'] == 4
  assert len(spec['input_shapes']) == 11
  assert len(spec['output_shapes']) == 6
  assert profile['vehicle'] == 'Hyundai NEXO (FE), 2019'
  assert profile['sources']['navigation'].startswith('Do not invent')
  assert profile['hud_prediction_fields'] == ['lane_lines', 'road_edges', 'lead', 'lead_prob', 'plan']
  assert profile['display_only'] is True
  assert profile['enabled'] is False
  assert profile['vehicle_control_enabled'] is False
  assert profile['hud_output_enabled'] is False
  assert profile['hud_geometry_verified'] is False
  assert all(profile['safety'][k] is False for k in
             ('auto_download', 'auto_install', 'auto_enable', 'live_hud_feed'))


def test_registry_never_claims_engine_hud_or_vehicle_approval():
  result = registry.report()
  assert result['state'] == 'registered_not_installed'
  assert not result['on_device_installed']
  assert not result['tensorrt_engine_verified']
  assert not result['model_inference_verified']
  assert not result['real_camera_verified']
  assert not result['hud_geometry_verified']
  assert not result['vehicle_control_enabled']
  assert not result['hud_output_enabled']


def test_streaming_hash_and_size_rejects_wrong_candidate(tmp_path):
  path = tmp_path / 'input.onnx'
  path.write_bytes(b'candidate bytes')
  digest = hashlib.sha256(path.read_bytes()).hexdigest()
  assert registry.hash_matches(path, sha256=digest, nbytes=15)
  assert not registry.hash_matches(path, sha256='0' * 64, nbytes=15)
  assert not registry.hash_matches(path, sha256=digest, nbytes=14)
  assert not registry.hash_matches(tmp_path / 'missing', sha256=digest, nbytes=15)
  link = tmp_path / 'link.onnx'
  link.symlink_to(path)
  assert not registry.hash_matches(link, sha256=digest, nbytes=15)
  assert registry.report(path)['state'] == 'local_artifact_invalid'


def test_registry_fails_closed_for_wrong_source_and_enabled_profile(tmp_path, monkeypatch):
  spec, profile = registry.load_registry()
  spec_file = tmp_path / 'copy.json'
  profile_file = tmp_path / 'profile.json'
  monkeypatch.setattr(registry, 'MODEL_SPEC', spec_file)
  monkeypatch.setattr(registry, 'NEXO_PROFILE', profile_file)
  profile_file.write_text(json.dumps(profile))
  bad = dict(spec)
  bad['sha256'] = '0' * 64
  spec_file.write_text(json.dumps(bad))
  with pytest.raises(ValueError, match='blob'):
    registry.load_registry()
  spec_file.write_bytes((Path(__file__).resolve().parents[1] / 'carrot_n16_spec.json').read_bytes())
  profile['enabled'] = True
  profile_file.write_text(json.dumps(profile))
  with pytest.raises(ValueError, match='disabled'):
    registry.load_registry()


def test_registry_only_imports_standard_library_and_has_no_control_entrypoint():
  source = Path(registry.__file__).read_text()
  assert 'sendcan' not in source
  assert 'CarControl' not in source
  assert 'SubMaster' not in source
  assert 'onnxruntime' not in source
  assert 'tensorrt' not in source.lower().replace("'tensorrt_engine_verified'", '')
  assert not hasattr(registry, 'activate_model')
  assert not hasattr(registry, 'install_model')
