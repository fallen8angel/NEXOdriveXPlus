"""Real original/candidate ONNX CPU evaluation plus exact numerical rejection tests."""
from pathlib import Path
import json

import numpy as np
import pytest

from openpilot.tools.jetson import model_accuracy as accuracy
from openpilot.tools.jetson.onnx_contract import inspect_model
from openpilot.tools.jetson.trt_diagnostics import MODEL_SHA

MODEL = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/models/driving_supercombo.onnx'


@pytest.mark.parametrize('index,label', [(0, 'outputs'), (917, 'lead'), (1061, 'lead_prob'),
                                         (1064, 'hidden_state'), (1576, 'plan')])
def test_localized_error_is_not_averaged_away(index, label):
  original = {'outputs': np.zeros((1, 2576), np.float16)}
  candidate = {'outputs': original['outputs'].copy()}
  candidate['outputs'][0, index] = 1
  result = accuracy.comparison(candidate, original)
  assert result[label]['passed'] is False and result[label]['outside_tolerance'] == 1
  assert result[label]['max_abs_error'] == 1
  assert result['outputs']['passed'] is False


@pytest.mark.parametrize('bad', ['nan', 'shape', 'dtype', 'name'])
def test_invalid_output_never_passes_via_numpy_broadcasting(bad):
  reference = {'outputs': np.zeros((1, 2576), np.float16)}
  value = {'outputs': reference['outputs'].copy()}
  if bad == 'nan':
    value['outputs'][0, 0] = np.nan
  elif bad == 'shape':
    value['outputs'] = value['outputs'][0]
  elif bad == 'dtype':
    value['outputs'] = value['outputs'].astype(np.float32)
  else:
    value = {'wrong': value['outputs']}
  with pytest.raises(ValueError, match='contract'):
    accuracy.comparison(value, reference)


def test_synthetic_cases_cover_image_range_and_nonzero_model_context():
  contract = inspect_model(MODEL, MODEL_SHA)
  values = [accuracy.feeds_for_case(contract, i) for i in range(accuracy.CASES)]
  assert len({accuracy.input_digest(v) for v in values}) == accuracy.CASES
  assert not np.any(values[0]['img']) and np.all(values[1]['img'] == 255)
  assert np.any(values[2]['features_buffer']) and np.any(values[2]['desire_pulse'])
  assert values[0]['traffic_convention'][0].tolist() == [1, 0]
  assert values[1]['traffic_convention'][0].tolist() == [0, 1]
  assert all(not value.flags.writeable for case in values for value in case.values())
  assert accuracy.input_digest(values[4]) == accuracy.input_digest(accuracy.feeds_for_case(contract, 4))


class Reference:
  evidence = {'backend': 'test-only', 'original_gpu_reference': False}
  def run(self, feeds):
    return {'outputs': np.zeros((1, 2576), np.float16)}


def test_three_way_gates_include_gpu_error_and_all_cases():
  contract = inspect_model(MODEL, MODEL_SHA)
  calls = []
  def gpu(feeds):
    calls.append(accuracy.input_digest(feeds))
    values = np.zeros((1, 2576), np.float16)
    values[0, 1576] = 1
    return {'outputs': values}
  result = accuracy.validate_cases(Reference(), Reference(), gpu, contract, samples=2)
  assert result['numerical_validation_passed'] is False
  assert result['worst_by_slice']['candidate_cpu_vs_original_cpu']['plan']['passed'] is True
  assert result['worst_by_slice']['candidate_gpu_vs_original_cpu']['plan']['passed'] is False
  assert len(result['cases']) == accuracy.CASES and len(set(calls[:accuracy.CASES])) == accuracy.CASES
  assert result['vehicle_approved'] is False and result['numerical_equivalence_verified'] is False
  assert result['timing']['measured_runs'] == 2


def test_real_original_and_fp32_gelu_cpu_outputs_are_compared_without_relabeling_as_gpu(tmp_path):
  import onnx
  from openpilot.tools.jetson.onnx_inputs import adapt
  from openpilot.tools.jetson.trt_conv_gelu_experiment import make_candidate
  contract = inspect_model(MODEL, MODEL_SHA)
  path = tmp_path / 'island.onnx'
  make_candidate(MODEL, path)
  onnx.save(adapt(onnx.load(path)), path)
  original, candidate = accuracy.CPUReference(MODEL), accuracy.CPUReference(path)
  try:
    rows = []
    for i in range(accuracy.CASES):
      feeds = accuracy.feeds_for_case(contract, i)
      reference, values = original.run(feeds), candidate.run(feeds)
      row = accuracy.comparison(values, reference)
      expected = np.abs(values['outputs'].astype(np.float64) - reference['outputs'].astype(np.float64))
      assert row['outputs']['max_abs_error'] == float(expected.max())
      assert row['outputs']['outside_tolerance'] == np.count_nonzero(
        expected > accuracy.POLICY['atol'] + accuracy.POLICY['rtol'] * np.abs(reference['outputs'].astype(np.float64)))
      rows.append(row)
    assert len(rows) == 8 and original.evidence['original_gpu_reference'] is False
    (tmp_path / 'cpu-accuracy-result.json').write_text(json.dumps({
      'source_sha256': MODEL_SHA, 'reference': original.evidence, 'cases': rows,
      'policy': accuracy.POLICY, 'gpu_verified': False, 'vehicle_approved': False}, indent=2))
  finally:
    candidate.close()
    original.close()
