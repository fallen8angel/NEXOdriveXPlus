"""Hosted CPU diagnostic: A/B the legacy handoff against the resolved N16 edges on frame zero."""
import argparse
import gc
import json
import multiprocessing
from pathlib import Path
import tempfile

import numpy as np
import onnx

from openpilot.tools.jetson.nexo_n16_cpu_plan import check_plan_space, source_contract, split_plan
from openpilot.tools.jetson.nexo_n16_cpu_precision import _feeds, cpu_session, image_adapter, widen_boundary
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import compare_array, load_arrays, save_arrays, synthetic_feeds
from openpilot.tools.jetson.nexo_n16_partition_extract import _find_candidate, extract_in_place
from openpilot.tools.jetson.nexo_n16_trt_trial import MODEL_BYTES, MODEL_SHA, digest, patch_stateful_images


def stage_run(role, source, stage, cut):
  from jetlink.onnx_patch import strip_tinygrad_ops
  if role in ('reference', 'old_left', 'old_right'):
    model = onnx.load(source)
    strip_tinygrad_ops(model)
    if role == 'reference':
      contract = source_contract(model, _find_candidate(model, cut)[1])
      (stage / 'contract.json').write_text(json.dumps(contract))
      session = cpu_session(model, save_plan=stage / 'execution.onnx')
    else:
      patch_stateful_images(model)
      candidate = _find_candidate(model, cut)[1]
      side = role.split('_')[1]
      extract_in_place(model, candidate, side)
      widen_boundary(model, candidate['boundary_tensors'], side)
      session = cpu_session(model)
    del model
    gc.collect()
  elif role == 'probe':
    proof = split_plan(stage, json.loads((stage / 'contract.json').read_text()))
    (stage / 'proof.json').write_text(json.dumps(proof))
    session = cpu_session(stage / 'probe.onnx')
  else:
    session = cpu_session(stage / (role + '.onnx'))

  if role == 'reference':
    feeds = synthetic_feeds(session.get_inputs(), 0)
    save_arrays(stage / 'inputs.npz', feeds)
  else:
    feeds = load_arrays(stage / 'inputs.npz')
  if role in ('right', 'old_right'):
    feeds.update(load_arrays(stage / ('left.npz' if role == 'right' else 'old_left.npz')))

  def evaluate(label):
    outputs = session.run(None, _feeds(session, feeds))
    save_arrays(stage / (label + '.npz'), dict(zip([v.name for v in session.get_outputs()], outputs, strict=True)))
  evaluate(role)
  if role == 'old_right':
    proof = json.loads((stage / 'proof.json').read_text())
    probe = load_arrays(stage / 'probe.npz')
    for edge in proof['boundary']:
      feeds[edge['name']] = probe[edge['tensor']].astype(np.float32)
    evaluate('old_right_with_reference')


def errors(expected, actual, *, legacy_images=False):
  if legacy_images and 'next_state_img_q' in expected:
    name = 'next_state_img_q'
    actual[name] = image_adapter(actual[name], expected[name].dtype)
  return {name: compare_array(value, actual[name], name)['max_abs_error'] for name, value in expected.items()}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('source', type=Path)
  parser.add_argument('cut', type=int, choices=(360, 361))
  args = parser.parse_args()
  if args.source.stat().st_size != MODEL_BYTES or digest(args.source) != MODEL_SHA:
    raise ValueError('Official N16 source required')
  with tempfile.TemporaryDirectory() as tmp:
    stage = Path(tmp)
    check_plan_space(stage, MODEL_BYTES)
    for role in ('reference', 'probe', 'left', 'right', 'old_left', 'old_right'):
      process = multiprocessing.get_context('spawn').Process(target=stage_run, args=(role, args.source, stage, args.cut))
      process.start()
      process.join()
      if process.exitcode != 0:
        raise RuntimeError('CPU diagnostic worker failed: ' + role)
    def load(role):
      return load_arrays(stage / (role + '.npz'))
    expected, probe = load('reference'), load('probe')
    proof = json.loads((stage / 'proof.json').read_text())
    old_left = load('old_left')
    left = load('left')
    edges = proof['boundary']
    def boundary_error(candidate, key):
      return {e['name']: compare_array(probe[e['tensor']].astype(np.float32),
                                       candidate[e[key]].astype(np.float32), e['name'])['max_abs_error'] for e in edges}
    result = {'cut': args.cut, 'source_sha256': MODEL_SHA, 'resolved_edges': edges,
              'probe_public': errors(expected, probe), 'old_left': boundary_error(old_left, 'name'),
              'old_public': errors(expected, {**old_left, **load('old_right')}, legacy_images=True),
              'old_right_with_reference': errors({k: v for k, v in expected.items() if k not in old_left},
                                                 load('old_right_with_reference')),
              'new_left': boundary_error(left, 'tensor'), 'new_public': errors(expected, {**left, **load('right')})}
    print(json.dumps(result, sort_keys=True), flush=True)
    if (max(result['probe_public'].values()) != 0 or max(result['new_left'].values()) > 0.005
        or max(result['new_public'].values()) > 0.005):
      raise ValueError('Resolved first-frame comparison failed')


if __name__ == '__main__':
  main()
