"""Actual ONNX extraction and pinned builder; GPU calls simulated explicitly."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import onnx
import pytest

from openpilot.tools.jetson import engine_experiments as exp
from openpilot.tools.jetson import prepare_engine as prep
from openpilot.tools.jetson import trt_diagnostics as diag
from openpilot.tools.jetson.tests.test_trt_diagnostics import builder as pinned_builder

MODEL = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/models/driving_supercombo.onnx'


@pytest.fixture
def builder(monkeypatch, tmp_path):
  return pinned_builder.__wrapped__(monkeypatch, tmp_path)


@pytest.fixture(scope='module')
def trials(tmp_path_factory):
  return exp.export_trials(MODEL, tmp_path_factory.mktemp('matrix') / 'models')


def test_exact_original_and_io_survive_experimental_exports(trials):
  assert prep.digest(MODEL) == diag.MODEL_SHA
  assert [row['name'] for row in trials] == list(exp.TRIALS)
  native, typed, primitive, fp32, no_flag, island, *regions, tactic, workspace = trials
  assert tactic['onnx'] == workspace['onnx'] == native['onnx']
  assert tactic['optimization_level'] == 5 and workspace['halve_workspace'] is True
  assert native['sha256'] == typed['sha256'] == no_flag['sha256']
  assert typed['onnx'] == native['onnx'] and typed['arithmetic_rewrite'] is False
  assert all(t['interface'] == native['interface'] for t in (typed, primitive, fp32, no_flag, island))
  assert primitive['arithmetic_rewrite'] and primitive['sha256'] != island['sha256']
  model = onnx.load(primitive['onnx'])
  assert not any(n.name == 'vision/node_gelu_1' for n in model.graph.node)
  assert any(n.name == 'nexo_diag_tanh_gelu/tanh' and n.op_type == 'Tanh' for n in model.graph.node)
  assert native['fp16_flag'] and not typed['fp16_flag'] and not no_flag['fp16_flag']
  assert island['arithmetic_rewrite'] is True
  for row, operators in zip(regions, [('Conv', 'Gelu'), ('Conv',), ('Gelu',)], strict=True):
    model = onnx.load(row['onnx'])
    assert tuple(node.op_type for node in model.graph.node) == operators
    assert not model.metadata_props and row['full_model'] is False
    onnx.checker.check_model(model, full_check=True)
  assert fp32['strict_fp32'] is True and fp32['fp16_flag'] is False and fp32['arithmetic_rewrite'] is True
  assert fp32['interface'] == trials[0]['interface']
  model = onnx.load(fp32['onnx'])
  assert not any(t.data_type == onnx.TensorProto.FLOAT16 for t in model.graph.initializer)
  casts_to_fp16 = [n for n in model.graph.node if n.op_type == 'Cast'
                   and any(a.name == 'to' and a.i == onnx.TensorProto.FLOAT16 for a in n.attribute)]
  assert len(casts_to_fp16) == len(model.graph.output)


def test_region_weights_and_math_are_exact_original_subgraphs(trials):
  from onnx.reference import ReferenceEvaluator
  from onnx.utils import Extractor
  original = onnx.shape_inference.infer_shapes(onnx.load(MODEL))
  expected = Extractor(original).extract_model(['vision/gelu'], ['vision/gelu_1'])
  extracted = onnx.load(next(t['onnx'] for t in trials if t['name'] == 'region_conv_gelu'))
  assert [n.SerializeToString() for n in expected.graph.node] == [n.SerializeToString() for n in extracted.graph.node]
  assert [w.SerializeToString() for w in expected.graph.initializer] == [w.SerializeToString() for w in extracted.graph.initializer]
  inputs = {'vision/gelu': np.random.default_rng(17).uniform(-2, 2, (1, 64, 64, 128)).astype(np.float16)}
  np.testing.assert_array_equal(ReferenceEvaluator(expected).run(None, inputs)[0],
                                ReferenceEvaluator(extracted).run(None, inputs)[0])


@pytest.mark.parametrize('name', ['native_no_fp16_flag', 'fp32_compute', 'native_strongly_typed'])
def test_trial_really_builds_loads_runs_checks_contract_and_never_approves_vehicle(builder, trials, tmp_path, monkeypatch, name):
  monkeypatch.setattr(exp, 'memory_snapshot', lambda: {'cuda_memory_available': False})
  trial = next(t for t in trials if t['name'] == name)
  builder['trt'].NetworkDefinitionCreationFlag = SimpleNamespace(STRONGLY_TYPED=1)
  class Engine:
    graph_exec = None
    cleanup_errors = []
    def __init__(self, path):
      assert Path(path).read_bytes() == b'simulated-plan'
      self.inputs = {n: SimpleNamespace(shape=v['shape'], dtype=np.float16, host=np.zeros(v['shape'], np.float16))
                     for n, v in trial['interface']['inputs'].items()}
      self.outputs = {'outputs': SimpleNamespace(shape=(1, 2576), dtype=np.float16)}
    def host_input(self, name):
      return self.inputs[name].host
    def warm(self):
      return 'simulated warmup'
    def run(self):
      return {'outputs': np.zeros((1, 2576), np.float16)}
    def close(self):
      pass
  row, values = exp.run_trial(trial, tmp_path / 'run', 2, builder=builder['vendor'], engine_type=Engine, trt=builder['trt'])
  assert builder['build_calls'] == 1 and 'FP16' not in builder['flags']
  assert builder.get('cleared_flags', []) == (['TF32', 'FP16'] if name == 'fp32_compute' else [])
  assert builder['network_flags'] == (2 if name == 'native_strongly_typed' else 0)
  assert json.loads((tmp_path / 'run/experiment.json').read_text())['strongly_typed'] == (name == 'native_strongly_typed')
  assert builder['vendor'].network_flags() == 0
  assert row['state'] == 'gpu_synthetic_passed' and row['measured_runs'] == 2
  assert row['production_cache_published'] is False and row['vehicle_approved'] is False
  assert row['numerical_equivalence_verified'] is False and 'reference_unavailable' in row
  assert row['synthetic_fps'] > 0
  assert row['validated_outputs']['outputs'] == {'shape': [1, 2576], 'dtype': 'float16', 'finite': True}
  assert values['outputs'].shape == (1, 2576)
  assert Path(row['build_log']).is_file() and prep.digest(MODEL) == diag.MODEL_SHA
  assert 'Tactic 42' in Path(row['verbose_log_path']).read_text()
  assert row['full_log_truncated'] is False and row['full_log_error'] is None


def test_failed_trial_preserves_diagnostics_without_loading_gpu(builder, trials, tmp_path, monkeypatch):
  builder['plan'] = None
  read_text = Path.read_text
  monkeypatch.setattr(Path, 'read_text', lambda p, *a, **kw: 'MemAvailable: 1024000 kB' if p == Path('/proc/meminfo') else read_text(p, *a, **kw))
  monkeypatch.setattr(exp, 'memory_snapshot', dict)
  row, values = exp.run_trial(trials[0], tmp_path / 'failed', 2, builder=builder['vendor'],
    engine_type=lambda *_: pytest.fail('failed build must not load an engine'), trt=builder['trt'])
  assert row['state'] == 'failed' and values is None
  assert row['failure_diagnosis']['root_cause_proven'] is False
  assert Path(row['build_log']).is_file() and row['build_memory']['samples'] >= 1


@pytest.mark.parametrize('name,level,budget', [('native_tactic_level5', 5, 512 << 20), ('native_half_workspace', 3, 256 << 20)])
def test_tactic_and_workspace_change_only_one_builder_setting(builder, trials, tmp_path, name, level, budget):
  trial = next(row for row in trials if row['name'] == name)
  builder['plan'] = None
  row, _ = exp.run_trial(trial, tmp_path / name, 2, builder=builder['vendor'], engine_type=None,
                         trt=builder['trt'], workspace=512 << 20)
  assert builder['config'].builder_optimization_level == level and builder['workspace'] == ('workspace', budget)
  assert builder['flags'] == ['FP16'] and row['failed_phase'] == 'building'


def test_experiment_parent_never_publishes_any_plan(tmp_path, monkeypatch):
  root = tmp_path / 'runtime'
  release = root / 'releases/selected'
  (release / prep.MODEL).parent.mkdir(parents=True)
  (release / prep.MODEL).write_bytes(b'original source')
  (root / 'current').symlink_to(release)
  (root / 'updates').mkdir()
  config = root / 'updates/nexo-ncm.json'
  config.write_text('{"display_only":true,"transport":"ncm-tcp"}')
  engines = root / 'cache/engines'
  engines.mkdir(parents=True)
  (engines / 'old.plan').write_bytes(b'preserved')
  (root / 'cache/nexo-engine-preparation.json').write_bytes(b'previous preparation')
  before = {p: p.read_bytes() for p in (config, engines / 'old.plan', root / 'cache/nexo-engine-preparation.json')}
  class Child:
    pid = None
    exitcode = 0
    def __init__(self, target, args):
      assert target is exp.worker
      self.stage = args[2]
    def start(self):
      prep.store(self.stage / 'result.json', {'state': 'passed', 'phase': 'experiment_matrix',
        'model_sha256': prep.digest(release / prep.MODEL), 'production_cache_published': False,
        'trials': [{'name': name, 'state': 'failed'} for name in exp.TRIALS]})
    def is_alive(self):
      return False
  monkeypatch.setattr(prep.multiprocessing, 'get_context', lambda _: SimpleNamespace(Process=Child))
  monkeypatch.setattr(prep.shutil, 'disk_usage', lambda _: SimpleNamespace(free=8 << 30))
  monkeypatch.setattr(prep, 'publish', lambda *a, **kw: pytest.fail('experiments never publish'))
  result = prep.prepare(root, lambda: True, experiment=True)
  assert result['state'] == 'experiment_complete' and result['production_cache_published'] is False
  assert {p: p.read_bytes() for p in before} == before
  assert [p.name for p in engines.iterdir()] == ['old.plan']
  assert json.loads((root / 'cache/nexo-engine-experiments.json').read_text())['experiment_only'] is True
  with pytest.raises(RuntimeError, match='Offroad'):
    prep.prepare(root, lambda: False, experiment=True)


def test_failed_build_restores_the_normal_input_adapter(builder, trials, tmp_path, monkeypatch):
  from jetlink import onnx_patch
  original = onnx_patch.patch_file
  builder['failure'] = RuntimeError('simulated build failure')
  monkeypatch.setattr(exp, 'memory_snapshot', dict)
  builder['trt'].NetworkDefinitionCreationFlag = SimpleNamespace(STRONGLY_TYPED=1)
  row, _ = exp.run_trial(trials[1], tmp_path / 'failed', 2, builder=builder['vendor'],
                         engine_type=lambda _: pytest.fail('not built'), trt=builder['trt'])
  assert row['state'] == 'failed' and onnx_patch.patch_file is original
  assert builder['network_flags'] == 2 and builder['vendor'].network_flags() == 0


def test_bounded_log_export_preserves_failure_details_without_accepting_path_escape(tmp_path):
  from openpilot.tools.jetson import manage
  stage = tmp_path / 'cache/nexo-experiment-abc'
  directory = stage / 'native_fp16'
  directory.mkdir(parents=True)
  (directory / 'experiment.build.json').write_text(json.dumps({'state': 'failed', 'workspace_bytes': 1024,
    'error': 'Could not find any implementation for node vision/node_conv2d_1 + vision/node_gelu_1',
    'warning_error_tail': [{'message': 'tactic failure'}], 'log_tail': [{'message': 'tactic evidence'}]}))
  report_path = tmp_path / 'cache/nexo-engine-experiments.json'
  prep.store(report_path, {'stage_directory': str(stage), 'state': 'experiment_complete',
                          'trials': [{'name': 'native_fp16', 'state': 'failed'}]})
  value = manage.experiment_logs(tmp_path)
  detail = value['experiments']['failed_build_excerpts'][0]
  assert detail['trial'] == 'native_fp16' and detail['verbose_tail'] == ['tactic evidence']
  assert detail['failure_diagnosis']['category'] == 'conv_gelu_tactic_unavailable'
  assert value['experiments']['production_cache_published'] is False
  prep.store(stage / 'result.json', {'current_trial': 'region_conv', 'trials': []})
  prep.store(report_path, {'stage_directory': str(stage), 'state': 'running'})
  live = manage.experiment_logs(tmp_path)['experiments']
  assert live['current_trial'] == 'region_conv'
  assert live['failed_build_excerpts'] == [{'trial': 'region_conv', 'state': 'awaiting_build_record'}]
  completed = {'name': 'native_strongly_typed', 'state': 'gpu_synthetic_passed', 'measured_runs': 20,
               'synchronized_inference_ms': {'p95': 12.0}}
  prep.store(report_path, {'stage_directory': str(stage), 'state': 'failed',
                           'candidate': {'trials': [completed], 'current_trial': 'fp32_compute'}})
  partial = manage.experiment_logs(tmp_path)['experiments']
  assert partial['trials'][0] == completed and partial['state'] == 'failed'
  assert partial['native_gpu_verified'] is False
  prep.store(report_path, {'stage_directory': str(tmp_path / '../elsewhere')})
  assert manage.experiment_logs(tmp_path)['ok'] is False


def test_reference_evaluator_error_does_not_erase_gpu_pass(builder, trials, tmp_path, monkeypatch):
  """An ONNX CPU reference failure is not a TensorRT compile/GPU failure."""
  from onnx import reference as onnx_reference
  monkeypatch.setattr(exp, 'memory_snapshot', lambda: {'cuda_memory_available': False})
  trial = next(row for row in trials if row['name'] == 'region_conv')
  class Engine:
    graph_exec = None
    def __init__(self, path):
      assert Path(path).read_bytes() == b'simulated-plan'
      def binding(value):
        dtype = np.dtype(onnx.helper.tensor_dtype_to_np_dtype(getattr(onnx.TensorProto, value['dtype'])))
        shape = tuple(value['shape'])
        return SimpleNamespace(shape=shape, dtype=dtype, host=np.zeros(shape, dtype))
      self.inputs = {name: binding(value) for name, value in trial['interface']['inputs'].items()}
      self.outputs = {name: binding(value) for name, value in trial['interface']['outputs'].items()}
    def host_input(self, name):
      return self.inputs[name].host
    def warm(self):
      return 'warm'
    def run(self):
      return {name: np.zeros(value.shape, value.dtype) for name, value in self.outputs.items()}
    def close(self):
      pass
  class FailingReference:
    def __init__(self, *args, **kwargs):
      raise RuntimeError('reference runtime unavailable')
  monkeypatch.setattr(onnx_reference, 'ReferenceEvaluator', FailingReference)
  row, values = exp.run_trial(trial, tmp_path / 'gpu-vs-cpu', 2, builder=builder['vendor'],
                              engine_type=Engine, trt=builder['trt'])
  assert row['state'] == 'gpu_synthetic_passed'
  assert row['reference_status'] == 'unavailable'
  assert 'reference runtime unavailable' in row['reference_error']
  assert 'reference_unavailable' in row and values is not None
  assert row['numerical_equivalence_verified'] is False
  assert row['vehicle_approved'] is False
  assert row['production_cache_published'] is False
