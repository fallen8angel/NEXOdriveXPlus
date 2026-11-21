"""Offline N16 CPU split-error localization; NO GPU/CAN/camera/HUD writes.

Only called by the OFFROAD, pinned-source, bounded-memory and disk guarded
nexo_n16_trt_trial. All probing ONNX copies live in a disposable stage.
"""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import (
  CASES, SLICES, compare_array, cpu_step_child, load_arrays, load_cpu_ort,
)
from openpilot.tools.jetson.nexo_n16_partition_extract import (
  N16_CUT, _node_digest, verify_exported_halves,
)

# The pinned N16 probe is ~815 MB. Allow the tiny-unit fixture to lower this
# floor explicitly, without loosening it on production Jetson.
MIN_PROBE_BYTES = 400_000_000


def instrument_reference(original: Path, output: Path, receipt_path: Path) -> dict:
  """Expose verified eight cut tensors without changing any arithmetic nodes."""
  import onnx
  from onnx import helper
  if (original.is_symlink() or not original.is_file() or
      output.exists() or output.is_symlink() or
      receipt_path.is_symlink() or not receipt_path.is_file() or
      receipt_path.stat().st_size > 128 * 1024):
    raise ValueError('Unsafe N16 original/probe path or receipt')
  receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
  tensors = receipt.get('boundary_tensors', [])
  names = [x.get('name') for x in tensors if isinstance(x, dict)]
  if (len(tensors) != 8 or len(names) != 8 or len(set(names)) != 8
      or receipt.get('cut_node_index') != N16_CUT
      or len(receipt.get('original_node_digest', '')) != 64):
    raise ValueError('N16 cut-364 eight-tensor contract changed')
  model = onnx.load(str(original))
  if (len(model.graph.node) != receipt['original_node_count'] or
      _node_digest(model.graph.node) != receipt['original_node_digest']):
    raise ValueError('Patched original graph differs from verified cut math')
  declared = {x.name for x in (*model.graph.input, *model.graph.output)}
  if declared & set(names):
    raise ValueError('Boundary overlaps original model input/output')
  for item in tensors:
    name, dtype, shape = item['name'], item['dtype'], item['shape']
    if (not isinstance(name, str) or not name or
        type(dtype) is not int or dtype <= 0 or
        not isinstance(shape, list) or not shape or
        any(type(dim) is not int or dim <= 0 for dim in shape)):
      raise ValueError('Invalid static boundary tensor type or shape')
    model.graph.output.append(helper.make_tensor_value_info(name, dtype, shape))
  if _node_digest(model.graph.node) != receipt['original_node_digest']:
    raise ValueError('Instrumenting original changed model operations')
  onnx.checker.check_model(model, full_check=False)
  onnx.save_model(model, str(output))
  if (output.is_symlink() or not output.is_file() or
      not MIN_PROBE_BYTES <= output.stat().st_size < 1_100_000_000):
    raise ValueError('Instrumented N16 size outside experimental bounds')
  return {'boundary_names': names, 'added_graph_outputs': 8,
          'original_math_unchanged': True, 'probe_model_disposable': True}


def _instrument_child(original: Path, output: Path, receipt: Path, stage: Path):
  from openpilot.tools.jetson.nexo_n16_diagnostics import save_json
  try:
    save_json(stage / 'probe-boundary.json',
              instrument_reference(original, output, receipt))
  except BaseException as error:
    save_json(stage / 'instrument-failure.json',
              {'error': f'{type(error).__name__}: {error}'[:450]})
    raise


def _run_child(job, *args):
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  child = multiprocessing.get_context('spawn').Process(target=job, args=args)
  child.start()
  child.join()
  if child.exitcode != 0:
    stage = args[-1] if job is _instrument_child else args[2]
    issue = ('instrument-failure.json' if job is _instrument_child
             else str(args[0]) + '-failure.json')
    reason = read_report(stage / issue).get('error') or f'worker exit {child.exitcode}'
    raise RuntimeError('Isolated N16 diagnostic failed: ' + str(reason)[:360])


def _compare_outputs(original, actual, expected_shapes: dict, name: str, *, subset=False):
  if (set(original) != set(expected_shapes) or
      (not set(actual) <= set(expected_shapes) if subset
       else not set(original) <= set(actual))):
    raise ValueError('Invalid pinned N16 output ownership: ' + name)
  compared = sorted(set(actual) if subset else set(original))
  if not compared:
    raise ValueError('No N16 final outputs to compare: ' + name)
  metrics, failed = {}, []
  for key in compared:
    if list(original[key].shape) != expected_shapes[key]:
      raise ValueError('Pinned N16 output shape changed: ' + key)
    metric = compare_array(original[key], actual[key], key)
    metrics[key] = metric
    if not metric['passed']:
      failed.append(key)
  if 'outputs' in compared:
    for part, (first, last) in SLICES.items():
      key = 'outputs:' + part
      metric = compare_array(original['outputs'][:, first:last],
                             actual['outputs'][:, first:last], key)
      metrics[key] = metric
      if not metric['passed']:
        failed.append(key)
  return {'failed_outputs': failed, 'metrics': metrics}


def compare_boundary_diagnosis(stage: Path, output_shapes: dict, boundary_names: list[str]) -> dict:
  """Quantify original-vs-probed, left boundary, right-only, whole chain.

  Observed errors NEVER become success by increasing tolerance. An altered
  reference due to exposing intermediate tensors is explicitly inconclusive.
  """
  if len(boundary_names) != 8 or len(set(boundary_names)) != 8:
    raise ValueError('Exactly eight pinned cut tensors are required')
  cases, probe_changed, left_bad, right_bad, chain_bad = [], False, False, False, False
  for case in range(CASES):
    original = load_arrays(stage / f'reference-{case}.npz')
    probe = load_arrays(stage / f'probe-reference-{case}.npz')
    real_boundary = load_arrays(stage / f'reference-boundary-{case}.npz')
    left = load_arrays(stage / f'left-{case}.npz')
    right = load_arrays(stage / f'right-{case}.npz')
    right_with_true_cut = load_arrays(stage / f'right_reference-{case}.npz')
    if set(real_boundary) != set(boundary_names) or not set(boundary_names) <= set(left):
      raise ValueError('Incomplete N16 boundary observation')
    boundary_scores = {}
    bad = []
    for name in boundary_names:
      score = compare_array(real_boundary[name], left[name], name)
      boundary_scores[name] = score
      if not score['passed']:
        bad.append(name)
    p = _compare_outputs(original, probe, output_shapes, 'reference_probe')
    r = _compare_outputs(original, right_with_true_cut, output_shapes,
                         'right_reference', subset=True)
    merged = {**left, **right}
    c = _compare_outputs(original, merged, output_shapes, 'full_split')
    probe_changed |= bool(p['failed_outputs'])
    left_bad |= bool(bad)
    right_bad |= bool(r['failed_outputs'])
    chain_bad |= bool(c['failed_outputs'])
    cases.append({'case': case, 'reference_probe': p,
                  'left_boundary': {'failed_tensors': bad, 'metrics': boundary_scores},
                  'right_using_true_boundary': r, 'full_split_chain': c})
  if probe_changed:
    location = 'inconclusive_instrumented_original_changed_outputs'
  elif left_bad and right_bad:
    location = 'both_halves_or_numeric_cut_semantics'
  elif left_bad:
    location = 'left_half_or_boundary_output_materialization'
  elif right_bad:
    location = 'right_half_or_boundary_input_materialization'
  elif chain_bad:
    location = 'boundary_handoff_or_combined_numeric_effect'
  else:
    location = 'no_difference_in_two_synthetic_cases'
  return {'state': 'boundary_diagnosis_complete', 'phase': 'n16_boundary_cpu_comparison',
          'candidate_location': location, 'reference_probe_unchanged': not probe_changed,
          'left_boundary_agrees': not left_bad,
          'right_using_true_boundary_agrees': not right_bad,
          'full_split_chain_agrees': not chain_bad, 'case_count': CASES,
          'boundary_tensor_count': len(boundary_names), 'cases': cases,
          'policy': {'atol': 0.005, 'rtol': 0.005, 'vehicle_acceptance': False},
          'numerical_parity_verified': False, 'gpu_inference_verified': False,
          'engine_build_attempted': False, 'vehicle_control_enabled': False,
          'hud_prediction_enabled': False, 'production_engine_published': False,
          'real_vehicle_validated': False}


def run_boundary_diagnosis(release: Path, stage: Path, on_phase):
  """Guard/lock/kill-on-memory-floor/cleanup belong to trial() parent."""
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report
  from openpilot.tools.jetson.nexo_n16_trt_trial import (
    ROOT, MODEL_SHA, MODEL_BYTES, _prepatch_worker,
  )
  from openpilot.tools.jetson.nexo_n16_registry import load_registry
  export = ROOT / 'cache' / ('nexo-n16-partition-export-' + MODEL_SHA[:12])
  if export.is_symlink() or not export.is_dir():
    raise ValueError('Verified N16 split export missing')
  checked = verify_exported_halves(export, N16_CUT)
  if checked.get('source_sha256') != MODEL_SHA:
    raise ValueError('Verified N16 split SHA changed')
  spec, _ = load_registry()
  if spec.get('sha256') != MODEL_SHA or spec.get('nbytes') != MODEL_BYTES:
    raise ValueError('Pinned original N16 source changed')
  receipt_path = export / 'left.json'
  if receipt_path.is_symlink() or not receipt_path.is_file():
    raise ValueError('Verified left half metadata missing')
  receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
  boundary_names = [x['name'] for x in receipt['boundary_tensors']]
  if len(boundary_names) != checked['boundary_tensor_count'] or len(boundary_names) != 8:
    raise ValueError('Pinned N16 split boundary changed')
  on_phase('boundary_cpu_runtime_preflight')
  load_cpu_ort()
  on_phase('boundary_prepare_reference')
  prep = multiprocessing.get_context('spawn').Process(
    target=_prepatch_worker, args=(release, stage))
  prep.start()
  prep.join()
  preparation = read_report(stage / 'prepatch-status.json')
  if prep.exitcode != 0 or preparation.get('ok') is not True:
    raise RuntimeError('Original N16 preprocessing failed: ' +
                       str(preparation.get('error') or prep.exitcode)[:320])
  original = stage / 'n16-prepatched.onnx'
  if original.is_symlink() or not original.is_file() or original.stat().st_size < 400_000_000:
    raise ValueError('Original temporary N16 graph missing')
  # Benchmark baseline without graph mutation BEFORE exposing intermediates.
  on_phase('boundary_reference_baseline')
  _run_child(cpu_step_child, 'reference', original, stage)
  augmented = stage / 'n16-original-with-boundary.onnx'
  on_phase('boundary_instrument_reference')
  _run_child(_instrument_child, original, augmented, receipt_path, stage)
  on_phase('boundary_reference_probe')
  _run_child(cpu_step_child, 'reference_probe', augmented, stage)
  # It is disposable; delete before evaluating the left/right halves to
  # reduce DATA pressure. Original and verified split ONNX stay unchanged.
  augmented.unlink()
  for mode, file in (('left', export / 'left.onnx'),
                     ('right', export / 'right.onnx'),
                     ('right_reference', export / 'right.onnx')):
    on_phase('boundary_' + mode + '_cpu')
    _run_child(cpu_step_child, mode, file, stage)
  on_phase('boundary_metrics')
  return {**compare_boundary_diagnosis(stage, spec['output_shapes'], boundary_names),
          'model_sha256': MODEL_SHA, 'left_sha256': checked['left_sha256'],
          'right_sha256': checked['right_sha256'],
          'original_math_unchanged': True, 'temporary_probe_deleted': True}
