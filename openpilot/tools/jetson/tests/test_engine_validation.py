"""CPU-only contract/measurement regression; these tests do not prove GPU execution."""
import hashlib
from types import SimpleNamespace

import numpy as np
import onnx
from onnx import TensorProto as T, helper
import pytest

from openpilot.tools.jetson import onnx_contract as contract
from openpilot.tools.jetson import protected_probe as probe


def save(path, dtype=T.FLOAT16, name='outputs', output_size=2, metadata='reference'):
  graph = helper.make_graph([helper.make_node('Cast', ['img'], [name], to=dtype)], 'test',
                            [helper.make_tensor_value_info('img', T.UINT8, [1, 2])],
                            [helper.make_tensor_value_info(name, dtype, [1, output_size])])
  model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 20)])
  helper.set_model_props(model, {'model_checkpoint': metadata})
  onnx.save(model, path)
  return hashlib.sha256(path.read_bytes()).hexdigest()


def test_matching_interface_does_not_claim_equivalence_or_vehicle_approval(tmp_path):
  a, b = tmp_path / 'original.onnx', tmp_path / 'candidate.onnx'
  first, second = save(a), save(b)
  report = contract.verify_pair(a, b, first, second)
  assert report['state'] == 'contract_matches' and report['exact_io_compatible']
  assert not report['numerical_equivalence_verified'] and not report['vehicle_approved']
  assert not report['tensorrt_engine_verified']


@pytest.mark.parametrize('change', [{'dtype': T.FLOAT}, {'name': 'different'}, {'metadata': 'other checkpoint'}])
def test_candidate_contract_change_is_diagnosed_and_not_approved(tmp_path, change):
  a, b = tmp_path / 'original.onnx', tmp_path / 'candidate.onnx'
  first, second = save(a), save(b, **change)
  report = contract.verify_pair(a, b, first, second)
  assert report['state'] == 'contract_mismatch' and report['differences']
  assert not report['vehicle_approved']


def test_corruption_fails_before_model_load_and_preserves_files(tmp_path):
  path = tmp_path / 'candidate.onnx'
  digest = save(path)
  path.write_bytes(path.read_bytes() + b'changed')
  before = path.read_bytes()
  with pytest.raises(ValueError, match='SHA-256 mismatch'):
    contract.inspect_model(path, digest)
  assert path.read_bytes() == before


def test_external_data_digest_cannot_masquerade_as_integrity(tmp_path):
  path = tmp_path / 'candidate.onnx'
  save(path)
  model = onnx.load(path)
  weight = onnx.TensorProto(name='external_weight', data_type=T.FLOAT, dims=[1])
  weight.data_location = T.EXTERNAL
  item = weight.external_data.add()
  item.key, item.value = 'location', 'weights.bin'
  model.graph.initializer.append(weight)
  path.write_bytes(model.SerializeToString())
  with pytest.raises(ValueError, match='External tensor'):
    contract.inspect_model(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_raw_engine_dtype_is_checked_before_any_wire_conversion():
  values = {'outputs': np.zeros((1, 2), np.float16)}
  probe.validate_outputs(values, {'outputs': (1, 2)}, {'outputs': np.float16})
  with pytest.raises(ValueError, match='contract'):
    probe.validate_outputs(values, {'outputs': (1, 2)}, {'outputs': np.float32})
  values['outputs'][0, 0] = np.inf
  with pytest.raises(ValueError, match='contract'):
    probe.validate_outputs(values, {'outputs': (1, 2)}, {'outputs': np.float16})


def test_benchmark_checks_warmups_and_every_output_and_excludes_warmup_timing(monkeypatch):
  prepared = []
  moments = iter([0., .010, 1., 1.002, 2., 2.004])
  monkeypatch.setattr(probe.time, 'perf_counter', lambda: next(moments))
  engine = SimpleNamespace(run=lambda: {'outputs': np.zeros((1, 2), np.float16)})
  report = probe.benchmark_engine(engine, prepared.append, {'outputs': (1, 2)}, {'outputs': np.float16}, warmups=1, samples=2)
  assert prepared == [0, 1, 2]
  assert report['synchronized_inference_ms']['mean'] == pytest.approx(3)
  assert report['measured_runs'] == 2 and not report['vehicle_inference_verified']
  engine.run = lambda: {'outputs': np.full((1, 2), np.nan, np.float16)}
  moments = iter([0., .01])
  with pytest.raises(ValueError, match='contract'):
    probe.benchmark_engine(engine, prepared.append, {'outputs': (1, 2)}, {'outputs': np.float16}, warmups=1, samples=2)


def test_native_model_raw_outputs_match_declared_fp16_contract():
  from pathlib import Path
  model_path = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/models/driving_supercombo.onnx'
  model = onnx.load(model_path)
  shapes = {value.name: tuple(d.dim_value for d in value.type.tensor_type.shape.dim) for value in model.graph.output}
  dtypes = {value.name: helper.tensor_dtype_to_np_dtype(value.type.tensor_type.elem_type) for value in model.graph.output}
  assert dtypes == {'outputs': np.dtype(np.float16)}
  outputs = {name: np.zeros(shape, dtype=dtypes[name]) for name, shape in shapes.items()}
  probe.validate_outputs(outputs, shapes, dtypes)
