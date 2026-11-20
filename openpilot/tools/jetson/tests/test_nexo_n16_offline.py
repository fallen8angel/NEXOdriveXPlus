"""N16 research-only offline staging; never accesses a live Jetson."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import nexo_n16_offline as offline
from openpilot.tools.jetson import nexo_n16_registry as registry


@pytest.fixture
def model(tmp_path, monkeypatch):
  contents = b'n16-test-only-fixture' * 8
  digest = hashlib.sha256(contents).hexdigest()
  source = tmp_path / 'model.onnx'
  source.write_bytes(contents)
  monkeypatch.setattr(registry, 'load_registry',
                      lambda: ({'sha256': digest, 'nbytes': len(contents)},
                               {'vehicle': 'Hyundai NEXO (FE), 2019'}))
  monkeypatch.setattr(offline.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024 ** 3))
  return source, digest, contents, tmp_path / 'research'


def test_read_only_does_not_create_cache_or_claim_inference(model):
  source, digest, contents, root = model
  result = offline.inspect(source, root=root)
  assert result['source_sha_verified'] is True
  assert result['model_staged'] is False
  assert result['state'] == 'source_verified_not_staged'
  assert result['gpu_inference_verified'] is False
  assert result['vehicle_control_enabled'] is False
  assert result['hud_prediction_enabled'] is False
  assert not root.exists()


def test_offroad_failure_blocks_staging(model):
  source, digest, contents, root = model
  with pytest.raises(offline.StagingError, match='Offroad'):
    offline.stage(source, lambda: False, root=root)
  assert not root.exists()


def test_stage_exact_existing_file_only_without_replacing(model):
  source, digest, contents, root = model
  outcome = offline.stage(source, lambda: True, root=root)
  assert outcome['action'] == 'staged'
  assert outcome['model_staged'] is True
  target = offline.staged_path(root, digest)
  assert target.read_bytes() == contents
  assert offline.stage(source, lambda: True, root=root)['action'] == 'already_staged'
  assert target.read_bytes() == contents
  assert source.read_bytes() == contents


def test_wrong_hash_and_existing_corruption_fail_closed(model):
  source, digest, contents, root = model
  source.write_bytes(b'x' * len(contents))
  with pytest.raises(offline.StagingError, match='SHA256'):
    offline.stage(source, lambda: True, root=root)
  assert not offline.staged_path(root, digest).exists()
  source.write_bytes(contents)
  target = offline.staged_path(root, digest)
  target.write_bytes(b'x' * len(contents))
  with pytest.raises(offline.StagingError, match='불일치'):
    offline.stage(source, lambda: True, root=root)
  assert target.read_bytes() != contents


def test_guard_failure_during_copy_removes_only_own_partial_file(model, monkeypatch):
  source, digest, contents, root = model
  monkeypatch.setattr(offline, 'CHUNK_SIZE', 4)
  calls = 0
  def guard():
    nonlocal calls
    calls += 1
    return calls < 3
  with pytest.raises(offline.StagingError, match='Offroad'):
    offline.stage(source, guard, root=root)
  assert list(root.glob('*.part')) == []
  assert not offline.staged_path(root, digest).exists()
  assert source.read_bytes() == contents


def test_not_enough_space_and_symlink_rejected(model, monkeypatch):
  source, digest, contents, root = model
  monkeypatch.setattr(offline.shutil, 'disk_usage',
                      lambda path: SimpleNamespace(free=len(contents)))
  with pytest.raises(offline.StagingError, match='2GiB'):
    offline.stage(source, lambda: True, root=root)
  symlink = source.with_name('linked.onnx')
  symlink.symlink_to(source)
  with pytest.raises(offline.StagingError, match='절대 경로'):
    offline.stage(symlink, lambda: True, root=root)


def test_stage_never_touches_production_models_or_can_paths():
  from pathlib import Path
  text = Path(offline.__file__).read_text()
  assert 'sendcan' not in text
  assert 'CarControl' not in text
  assert 'modeld' in text  # documentation only
  assert 'import tensorrt' not in text.lower()
  assert not hasattr(offline, 'enable_model')
