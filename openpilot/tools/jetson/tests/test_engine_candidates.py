"""Real artifact hashes/graphs/registration, simulated GPU and Offroad signals only."""
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.tools.jetson import engine_candidates as candidates, prepare_engine as prep
from openpilot.tools.jetson.trt_diagnostics import MODEL_SHA

MODEL = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/models/driving_supercombo.onnx'


@pytest.fixture(scope='module')
def canonical_models(tmp_path_factory):
  import onnx
  from openpilot.tools.jetson.onnx_inputs import adapt
  from openpilot.tools.jetson.trt_conv_gelu_experiment import make_candidate
  directory = tmp_path_factory.mktemp('candidate-model-snapshots')
  source, candidate = directory / 'source.onnx', directory / 'candidate.onnx'
  # Shared snapshots belong only to these tests; never hard-link the repository model.
  source.write_bytes(MODEL.read_bytes())
  make_candidate(source, candidate)
  onnx.save(adapt(onnx.load(candidate)), candidate)
  yield source, candidate
  shutil.rmtree(directory)


@pytest.fixture
def artifacts(tmp_path, canonical_models):
  root = tmp_path / 'runtime'
  release = root / 'releases/source'
  source = release / prep.MODEL
  source.parent.mkdir(parents=True)
  os.link(canonical_models[0], source)
  (release / 'NEXO_RUNTIME.json').write_text(json.dumps({'source_commit': 'a' * 40,
    'runtime': {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'}}))
  (root / 'current').symlink_to(release)
  (root / 'updates').mkdir()
  (root / 'updates/nexo-ncm.json').write_text('{"display_only":true,"transport":"ncm-tcp"}')
  experiment = root / 'cache/nexo-experiment-existing'
  models = experiment / 'models'
  models.mkdir(parents=True)
  model = models / 'gelu_fp32_island.onnx'
  os.link(canonical_models[1], model)
  trial = experiment / 'gelu_fp32_island'
  trial.mkdir()
  (trial / 'experiment.plan').write_bytes(b'PREEXISTING GPU PLAN - SIMULATED IN TEST ONLY')
  (trial / 'experiment.json').write_text(json.dumps({'source_model_sha256': MODEL_SHA, 'experiment_only': True,
    'trt_version': '10.3.0', 'device': 'Orin-sm87'}))
  (trial / 'result.json').write_text(json.dumps({'name': 'gelu_fp32_island', 'state': 'gpu_synthetic_passed',
    'full_model': True, 'arithmetic_rewrite': True, 'sha256': prep.digest(model),
    'plan_sha256': prep.digest(trial / 'experiment.plan')}))
  stage = root / 'cache/nexo-validation-existing'
  stage.mkdir()
  yield root, release, trial, stage
  shutil.rmtree(root)


class FakeReference:
  evidence = {'backend': 'test-only-not-real-gpu', 'original_gpu_reference': False}
  def __init__(self, path):
    pass
  def run(self, feeds):
    return {'outputs': np.zeros((1, 2576), np.float16)}
  def close(self):
    pass


class FakeGPU:
  def __init__(self, path):
    from openpilot.tools.jetson.onnx_contract import inspect_model
    contract = inspect_model(MODEL, MODEL_SHA)
    self.inputs = {v['name']: SimpleNamespace(shape=v['shape'], dtype=np.float16)
                   for v in contract['inputs']}
    self.outputs = {'outputs': SimpleNamespace(shape=(1, 2576), dtype=np.float16)}
    self.buffers = {k: np.zeros(v.shape, v.dtype) for k, v in self.inputs.items()}
  def host_input(self, name):
    return self.buffers[name]
  def warm(self):
    pass
  def run(self):
    return {'outputs': np.zeros((1, 2576), np.float16)}
  def close(self):
    pass


def validated(artifacts):
  root, release, trial, stage = artifacts
  proof = candidates.validate_existing(root, release, stage, trial, 2, FakeGPU, FakeReference)
  proof.update(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), gpu_device='Orin-sm87')
  prep.store(stage / 'result.json', proof)
  prep.store(root / 'cache/nexo-engine-validation.json', {'state': 'validation_complete', 'stage_directory': str(stage),
    'worker_exitcode': 0, 'validation_result_sha256': prep.digest(stage / 'result.json')})
  return proof


def test_validate_reuses_existing_plan_and_registers_only_inactive_metadata(artifacts):
  root, release, trial, stage = artifacts
  old = {p: p.read_bytes() for p in trial.iterdir()}
  config = (root / 'updates/nexo-ncm.json').read_bytes()
  validated(artifacts)
  assert old == {p: p.read_bytes() for p in trial.iterdir()}
  receipt = candidates.register(root, stage, lambda: True)
  assert receipt['active'] is False and receipt['vehicle_approved'] is False
  assert receipt['recorded_driving_verified'] is False and receipt['production_cache_published'] is False
  assert (root / 'current').resolve() == release and (root / 'updates/nexo-ncm.json').read_bytes() == config
  assert not (root / 'cache/engines').exists() and len(list((root / 'cache/engine-candidates').iterdir())) == 1
  assert candidates.register(root, stage, lambda: True) == receipt


@pytest.mark.parametrize('kind', ['plan', 'metadata', 'trial_result', 'onnx'])
def test_tampered_existing_artifact_blocks_validation(artifacts, kind):
  root, release, trial, stage = artifacts
  paths = candidates.artifact_paths(root, trial)
  changed = paths[kind].read_bytes() + b'changed'
  paths[kind].unlink()  # Break shared test snapshot links before perturbing a model.
  paths[kind].write_bytes(changed)
  with pytest.raises((ValueError, OSError)):
    candidates.verify_artifacts(root, release, trial, stage)
  assert not (root / 'cache/engine-candidates').exists()


@pytest.mark.parametrize('failure', ['accuracy', 'plan_changed', 'offroad', 'config', 'no_supervisor', 'reboot'])
def test_registration_failures_preserve_source_plan_and_settings(artifacts, failure):
  root, release, trial, stage = artifacts
  proof = validated(artifacts)
  def gate():
    return failure != 'offroad'
  if failure in ('accuracy', 'reboot'):
    proof['numerical_validation_passed' if failure == 'accuracy' else 'boot_id'] = False if failure == 'accuracy' else 'old-boot'
    prep.store(stage / 'result.json', proof)
    prep.store(root / 'cache/nexo-engine-validation.json', {'state': 'validation_complete', 'stage_directory': str(stage),
      'worker_exitcode': 0, 'validation_result_sha256': prep.digest(stage / 'result.json')})
  elif failure == 'plan_changed':
    (trial / 'experiment.plan').write_bytes(b'changed plan')
  elif failure == 'offroad':
    pass
  elif failure == 'config':
    (root / 'updates/nexo-ncm.json').write_text('{"display_only":false}')
  else:
    (root / 'cache/nexo-engine-validation.json').unlink()
  before = (root / 'updates/nexo-ncm.json').read_bytes()
  with pytest.raises((RuntimeError, ValueError, OSError)):
    candidates.register(root, stage, gate)
  assert (root / 'current').resolve() == release
  assert (root / 'updates/nexo-ncm.json').read_bytes() == before
  assert not list((root / 'cache').glob('engine-candidates/*.json'))


def test_offroad_loss_at_publication_preserves_previous_receipt(artifacts):
  root, _, _, stage = artifacts
  validated(artifacts)
  answers = iter([True, False])
  with pytest.raises(RuntimeError, match='changed'):
    candidates.register(root, stage, lambda: next(answers))
  assert list((root / 'cache/engine-candidates').iterdir()) == []


def test_registry_conflict_never_overwrites_existing_record(artifacts):
  root, _, _, stage = artifacts
  validated(artifacts)
  registry = root / 'cache/engine-candidates'
  registry.mkdir()
  path = registry / (prep.digest(stage / 'result.json') + '.json')
  path.write_text('{"old":"preserved"}')
  with pytest.raises(FileExistsError):
    candidates.register(root, stage, lambda: True)
  assert path.read_text() == '{"old":"preserved"}'


def test_wrong_trial_or_symlink_is_never_followed(artifacts):
  root, _, trial, _ = artifacts
  with pytest.raises(ValueError):
    candidates.artifact_paths(root, trial.parent / 'region_gelu')
  original = trial / 'experiment.plan'
  other = root / 'outside.plan'
  original.rename(other)
  original.symlink_to(other)
  with pytest.raises(ValueError):
    candidates.artifact_paths(root, trial)


def test_diagnostic_read_never_imports_cuda_revalidates_or_registers(artifacts, monkeypatch):
  from openpilot.tools.jetson import manage
  root, _, _, stage = artifacts
  validated(artifacts)
  candidates.register(root, stage, lambda: True)
  monkeypatch.setattr(candidates, 'register', lambda *a: (_ for _ in ()).throw(AssertionError('read only')))
  monkeypatch.setattr(candidates, 'validate_existing', lambda *a: (_ for _ in ()).throw(AssertionError('read only')))
  before = sorted((str(p), p.stat().st_size) for p in (root / 'cache').rglob('*') if p.is_file())
  value = manage.candidate_logs(root)['engine_candidate']
  assert value['active'] is False and value['vehicle_approved'] is False
  assert value['artifact_integrity_rechecked'] is False and value['recorded_evidence_only'] is True
  assert len(value['registered_candidates']) == 1
  assert before == sorted((str(p), p.stat().st_size) for p in (root / 'cache').rglob('*') if p.is_file())


def test_missing_cpu_reference_fails_before_gpu_load_with_a_specific_phase(artifacts):
  root, release, trial, stage = artifacts
  phases = []
  def missing(path):
    raise ModuleNotFoundError('onnxruntime required', name='onnxruntime')
  def no_gpu(path):
    raise AssertionError('GPU must not load before CPU reference')
  with pytest.raises(ModuleNotFoundError):
    candidates.validate_existing(root, release, stage, trial, 2, no_gpu, missing, phases.append)
  assert phases[-1] == 'cpu_reference_loading'
  assert not (root / 'cache/engine-candidates').exists()
