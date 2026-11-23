"""Isolated recurrent CPU validation, with legacy FP32 transport for cut 364.

ORT may promote individual FP16 kernels even with graph optimizations disabled.
A graph-output FP16 boundary adds a rounding point absent from the whole CPU
graph. Cast-only transport keeps original weights, operators and public IO;
it is not permission to build, publish or activate a GPU engine.
"""
from __future__ import annotations

from pathlib import Path
import time

FRAMES = 140
IMAGE_INPUTS = frozenset(('new_img', 'state_img_q'))


def widen_boundary(model, tensors: list[dict], side: str) -> dict:
  from onnx import TensorProto, checker, helper
  from openpilot.tools.jetson.nexo_n16_partition_extract import _node_digest
  if side not in ('left', 'right') or not tensors or len(tensors) > 48:
    raise ValueError('Invalid CPU precision boundary')
  graph = model.graph
  names = [item['name'] for item in tensors]
  declared = {v.name: v for v in (graph.output if side == 'left' else graph.input)}
  if len(set(names)) != len(names) or any(name not in declared for name in names):
    raise ValueError('Boundary is not owned by selected partition')
  if any(item['dtype'] != TensorProto.FLOAT16 or
         declared[item['name']].type.tensor_type.elem_type != TensorProto.FLOAT16
         for item in tensors):
    raise ValueError('Only verified FP16 boundaries may be widened')
  occupied = {v.name for v in (*graph.input, *graph.output, *graph.value_info, *graph.initializer)}
  occupied.update(name for node in graph.node for name in (*node.input, *node.output))
  internal = {name: name + '__nexo_cpu_fp16' for name in names}
  if occupied & set(internal.values()):
    raise ValueError('CPU precision wrapper name collision')
  original_digest = _node_digest(graph.node)
  # Rename only the private side of the boundary, never any public N16 IO.
  for node in graph.node:
    for index, name in enumerate(node.input):
      if name in internal:
        node.input[index] = internal[name]
    if side == 'left':
      for index, name in enumerate(node.output):
        if name in internal:
          node.output[index] = internal[name]
  for value in graph.value_info:
    if value.name in internal:
      value.name = internal[value.name]
  wrappers = []
  for item in tensors:
    name = item['name']
    declared[name].type.tensor_type.elem_type = TensorProto.FLOAT
    source, target = (internal[name], name) if side == 'left' else (name, internal[name])
    wrappers.append(helper.make_node('Cast', [source], [target], name='nexo_cpu_handoff_' + name,
                                    to=TensorProto.FLOAT if side == 'left' else TensorProto.FLOAT16))
  if side == 'left':
    graph.node.extend(wrappers)
  else:
    for node in reversed(wrappers):
      graph.node.insert(0, node)
  layout_moves = 0
  if side == 'right':
    # A native FP16 Transpose immediately after a cut would materialize the
    # Cast before the next promoted arithmetic kernel (observed at cut 361).
    # Commute only through a single-consumer pure permutation. No arithmetic,
    # multi-consumer tensor or public output is moved across a Cast.
    while True:
      for cast in list(graph.node):
        if cast.op_type != 'Cast' or not cast.name.startswith('nexo_cpu_handoff_'):
          continue
        users = [node for node in graph.node if cast.output[0] in node.input]
        if len(users) != 1:
          continue
        node = users[0]
        if (node.domain or node.op_type != 'Transpose' or len(node.input) != 1 or len(node.output) != 1
            or node.output[0] in {v.name for v in graph.output}):
          continue
        target = node.output[0]
        alias = target + '__nexo_cpu_fp32_transport'
        if alias in occupied:
          raise ValueError('CPU layout wrapper name collision')
        occupied.add(alias)
        node.input[0], node.output[0] = cast.input[0], alias
        cast.input[0], cast.output[0] = alias, target
        graph.node.remove(cast)
        position = next(i for i, value in enumerate(graph.node) if alias in value.output)
        graph.node.insert(position + 1, cast)
        layout_moves += 1
        break
      else:
        break
  checker.check_model(model, full_check=False)
  return {'mode': 'explicit_fp32_boundary_transport', 'side': side,
          'original_node_digest': original_digest, 'boundary_names': names,
          'cast_count': len(wrappers), 'layout_cast_moves': layout_moves, 'weights_changed': False,
          'gpu_inference_verified': False, 'vehicle_control_enabled': False}


def cpu_session(model, *, save_plan=None):
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import load_cpu_ort
  ort = load_cpu_ort()
  opts = ort.SessionOptions()
  opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
  opts.intra_op_num_threads = opts.inter_op_num_threads = 1
  opts.enable_cpu_mem_arena = opts.enable_mem_pattern = False
  if save_plan is not None:
    opts.optimized_model_filepath = str(save_plan)
    opts.add_session_config_entry('session.optimized_model_external_initializers_file_name', 'weights.bin')
    opts.add_session_config_entry('session.optimized_model_external_initializers_min_size_in_bytes', '65536')
  data = str(model) if isinstance(model, Path) else model.SerializeToString()
  session = ort.InferenceSession(data, opts, providers=['CPUExecutionProvider'])
  if session.get_providers() != ['CPUExecutionProvider']:
    raise ValueError('CPU-only N16 execution required')
  return session


def image_adapter(value, dtype):
  """The ONLY public dtype adapter: exact 0..255 image bytes <-> FP16."""
  import numpy as np
  if (value.dtype not in (np.dtype('uint8'), np.dtype('float16'))
      or dtype not in (np.dtype('uint8'), np.dtype('float16'))
      or not np.isfinite(value).all() or np.any(value < 0) or np.any(value > 255)
      or np.any(value != np.floor(value))):
    raise ValueError('Image adapter requires exact UINT8-range values')
  return value.astype(dtype, copy=False)


def _feeds(session, available):
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import bind_inputs, ort_dtype
  values = dict(available)
  for item in session.get_inputs():
    if item.name in IMAGE_INPUTS and item.name in values:
      values[item.name] = image_adapter(values[item.name], ort_dtype(item.type))
  return bind_inputs(session.get_inputs(), values)


def sequence_feeds(seeds, frames):
  """Identical RNG order for concurrent tests and isolated reference/replay workers."""
  import numpy as np
  rng = np.random.default_rng(20261121)
  for frame in range(frames):
    feeds = {k: v.copy() for k, v in seeds.items() if not k.startswith('state_')}
    for name, value in feeds.items():
      if frame == 0:
        continue
      if name in ('new_img', 'nav_map'):
        value[...] = rng.integers(0, 256, value.shape, dtype=np.uint16)
      elif name in ('desire', 'traffic_convention'):
        value.reshape(-1)[(frame // 7) % value.size] = 1
      else:
        value[...] = rng.uniform(-0.125, 0.125, value.shape)
    yield frame, feeds


def compare_sequence(reference, left, right, spec, on_frame=lambda frame: None, *, frames=FRAMES, first_boundary=None):
  """Independent recurrent chains, all public tensors and ALL declared slices."""
  import hashlib
  import numpy as np
  import resource
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import compare_array, synthetic_feeds, POLICY
  if type(frames) is not int or not 1 <= frames <= FRAMES:
    raise ValueError('Invalid bounded CPU frame count')
  seeds = synthetic_feeds(reference.get_inputs(), 0)
  states = {k: v for k, v in seeds.items() if k.startswith('state_')}
  reference_state = {k: v.copy() for k, v in states.items()}
  candidate_state = {k: v.copy() for k, v in states.items()}
  shapes = spec['output_shapes']
  state_outputs = {name: 'next_' + name for name in states}
  if not set(state_outputs.values()) <= set(shapes):
    raise ValueError('Incomplete recurrent output mapping')
  maxima, failed, timings, hashes = {}, [], [], []
  first_boundary_errors = {}
  max_buffers = {'input_bytes': 0, 'left_output_bytes': 0, 'right_output_bytes': 0}
  for frame, feeds in sequence_feeds(seeds, frames):
    on_frame(frame)
    hashes.append(hashlib.sha256(feeds['new_img'].tobytes()).hexdigest())
    start = time.monotonic()
    expected = dict(zip([v.name for v in reference.get_outputs()],
                        reference.run(None, _feeds(reference, {**feeds, **reference_state})), strict=True))
    baseline_ms = getattr(reference, 'last_inference_ms', (time.monotonic() - start) * 1000)
    start = time.monotonic()
    values = {**feeds, **candidate_state}
    l = dict(zip([v.name for v in left.get_outputs()], left.run(None, _feeds(left, values)), strict=True))
    if frame == 0 and first_boundary is not None:
      for name, baseline in first_boundary.items():
        metric = compare_array(baseline, l[name], name)
        first_boundary_errors[name] = metric['max_abs_error']
        if not metric['passed']:
          failed.append({'frame': frame, 'output': 'boundary:' + name, 'max_abs_error': metric['max_abs_error']})
    r = dict(zip([v.name for v in right.get_outputs()], right.run(None, _feeds(right, {**values, **l})), strict=True))
    for key, tensors in (('input_bytes', values), ('left_output_bytes', l), ('right_output_bytes', r)):
      max_buffers[key] = max(max_buffers[key], sum(v.nbytes for v in tensors.values()))
    if set(l) & set(r):
      raise ValueError('Duplicate partition output ownership')
    actual = {**l, **r}
    if set(expected) != set(shapes) or not set(shapes) <= set(actual):
      raise ValueError('Missing original N16 output')
    if 'next_state_img_q' in actual:
      actual['next_state_img_q'] = image_adapter(actual['next_state_img_q'], expected['next_state_img_q'].dtype)
    timings.append({'reference_ms': baseline_ms, 'split_ms': (time.monotonic() - start) * 1000})
    metrics = {}
    for name, shape in shapes.items():
      if list(expected[name].shape) != shape:
        raise ValueError('Original N16 output shape changed: ' + name)
      metrics[name] = compare_array(expected[name], actual[name], name)
    for name, (first, last) in spec['output_slices'].items():
      key = 'outputs:' + name
      metrics[key] = compare_array(expected['outputs'][:, first:last], actual['outputs'][:, first:last], key)
    for name, metric in metrics.items():
      maxima[name] = max(maxima.get(name, 0.0), metric['max_abs_error'])
      if not metric['passed']:
        failed.append({'frame': frame, 'output': name, 'max_abs_error': metric['max_abs_error']})
    # Each path consumes its OWN outputs; never reset or feed reference state to the candidate.
    reference_state = {name: expected[out] for name, out in state_outputs.items()}
    candidate_state = {name: actual[out] for name, out in state_outputs.items()}
  complete = frames == FRAMES and not failed
  performance = {}
  for key in ('reference_ms', 'split_ms'):
    values = np.array([row[key] for row in timings])
    performance[key] = {'mean': float(values.mean()), 'p50': float(np.percentile(values, 50)),
                        'p95': float(np.percentile(values, 95)), 'max': float(values.max())}
  return {'state': 'cpu_parity_passed' if complete else 'cpu_parity_failed',
          'phase': 'n16_stateful_cpu_fp32_handoff', 'frames_completed': frames,
          'stateful_140_frames_verified': complete, 'cpu_numerical_parity_verified': complete,
          'numerical_parity_verified': complete, 'policy': dict(POLICY),
          'dataset_scope': 'synthetic_zero_then_varied_images_desire_navigation_independent_recurrence',
          'first_frame_boundary_max_abs_error': first_boundary_errors,
          'distinct_image_frames': len(set(hashes)), 'max_abs_error_by_output': maxima,
          'failed_output_count': len(failed), 'failed_outputs': failed[:48],
          'performance_cpu_only_ms': performance,
          'maximum_tensor_buffers_bytes': max_buffers,
          'peak_process_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
          'raw_original_uint8_reference': True, 'image_adapter_lossless': True,
          'gpu_inference_verified': False, 'engine_build_attempted': False,
          'vehicle_control_enabled': False, 'hud_prediction_enabled': False,
          'production_engine_published': False, 'real_vehicle_validated': False}


def build_sessions(source: Path, release: Path, cut: int, sides, on_phase, *, stage: Path):
  """Build only this phase's sessions. Used inside short-lived CPU workers."""
  import gc
  import sys
  import onnx
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import digest
  from openpilot.tools.jetson.nexo_n16_registry import load_registry
  from openpilot.tools.jetson.nexo_n16_partition_extract import ALLOWED_N16_CUTS, _find_candidate, extract_in_place
  from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images
  spec, _ = load_registry()
  if sides not in (('reference',), ('left', 'right')):
    raise ValueError('CPU sessions must be reference-only or split-only')
  if (type(cut) is not int or cut not in ALLOWED_N16_CUTS or source.is_symlink()
      or not source.is_file() or source.stat().st_size != spec['nbytes'] or digest(source) != spec['sha256']):
    raise ValueError('Pinned N16 source identity or CPU cut changed')
  sys.path.insert(0, str(release / 'carrot-server/third_party/jetlink'))
  from jetlink.onnx_patch import strip_tinygrad_ops
  from openpilot.tools.jetson.nexo_n16_cpu_plan import check_plan_space, source_contract
  from openpilot.tools.jetson.nexo_n16_diagnostics import save_json, read_report
  sessions, receipts = [], []
  resolved = cut in (360, 361)
  if resolved and sides == ('left', 'right'):
    on_phase('stateful_cpu_resolve_boundary')
    proof = read_report(stage / 'probe-result.json')['plan']
    for side in sides:
      on_phase('stateful_cpu_prepare_' + side)
      sessions.append(cpu_session(stage / (side + '.onnx')))
      on_phase('stateful_cpu_session_live_' + side)
    return sessions, [proof], spec
  for side in sides:
    on_phase('stateful_cpu_prepare_' + side)
    model = onnx.load(str(source))
    on_phase('stateful_cpu_graph_loaded_' + side)
    strip_tinygrad_ops(model)
    if resolved:
      check_plan_space(stage, source.stat().st_size)
      _, candidate = _find_candidate(model, cut)
      save_json(stage / 'contract.json', source_contract(model, candidate))
    elif side != 'reference':
      patch_stateful_images(model)
      _, candidate = _find_candidate(model, cut)
      receipt = extract_in_place(model, candidate, side)
      receipt['transport'] = widen_boundary(model, candidate['boundary_tensors'], side)
      receipts.append(receipt)
    sessions.append(cpu_session(model, save_plan=stage / 'execution.onnx' if resolved else None))
    on_phase('stateful_cpu_session_live_' + side)
    del model
    gc.collect()
    on_phase('stateful_cpu_graph_released_' + side)
  if sides == ('reference',) and (
      {v.name: v.shape for v in sessions[0].get_inputs()} != spec['input_shapes']
      or {v.name: v.shape for v in sessions[0].get_outputs()} != spec['output_shapes']):
    raise ValueError('Raw original N16 input/output contract changed')
  return sessions, receipts, spec


def run_stateful_cpu(source: Path, release: Path, cut: int, on_phase, *, stage: Path | None = None):
  """Release all reference memory by process exit before loading either half."""
  from openpilot.tools.jetson.nexo_n16_cpu_workers import run_serial_cpu
  return run_serial_cpu(source, release, cut, on_phase, stage=stage)
