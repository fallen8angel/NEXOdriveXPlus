"""Offroad trial internals. Build cut 360 in scratch and compare against raw CPU replay."""
from collections import deque
from contextlib import ExitStack
import os
import shutil
import sys
import time
from types import SimpleNamespace

from openpilot.tools.jetson.engine_experiments import memory_snapshot
from openpilot.tools.jetson.nexo_n16_cpu_precision import FRAMES, compare_sequence
from openpilot.tools.jetson.nexo_n16_cpu_workers import ReferenceReplay, _cpu_child, isolated_phase
from openpilot.tools.jetson.nexo_n16_diagnostics import read_report, save_json
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import POLICY, load_arrays
from openpilot.tools.jetson.nexo_n16_registry import load_registry
from openpilot.tools.jetson.nexo_n16_trt_trial import DISK_FLOOR, MODEL_SHA, TRIAL_TIMEOUT_S, digest, patch_stateful_images


def accepted_cpu_plan(report, spec):
  plan = report.get('boundary_transport') or {}
  edges = report.get('first_frame_boundary_max_abs_error') or {}
  errors = report.get('max_abs_error_by_output') or {}
  expected = set(spec['output_shapes']) | {'outputs:' + name for name in spec['output_slices']}
  if not (report.get('state') == 'cpu_parity_passed' and report.get('model_sha256') == MODEL_SHA
          and report.get('cut_node_index') == 360 and report.get('policy') == POLICY
          and report.get('frames_completed') == report.get('distinct_image_frames') == FRAMES
          and report.get('cpu_numerical_parity_verified') is True and report.get('stateful_140_frames_verified') is True
          and report.get('initializer_integrity_verified') is True and report.get('failed_output_count') == 0
          and plan.get('mode') == 'resolved_whole_cpu_graph' and plan.get('probe_public_outputs_exact') is True
          and len(edges) == 9 and set(edges) == {edge['tensor'] for edge in plan.get('boundary', [])}
          and set(errors) == expected and all(type(v) in (float, int) and 0 <= v <= POLICY['atol']
                                               for v in (*errors.values(), *edges.values()))):
    raise ValueError('A passing local cut-360 resolved CPU receipt for the pinned N16 and all 140 frames is required')
  return plan


def half_contract(path):
  import onnx
  model = onnx.load(path, load_external_data=False)
  def descriptors(items):
    return {v.name: SimpleNamespace(name=v.name, shape=[d.dim_value for d in v.type.tensor_type.shape.dim],
                                    type='tensor(' + onnx.TensorProto.DataType.Name(v.type.tensor_type.elem_type).lower() + ')')
            for v in items}
  return descriptors(model.graph.input), descriptors(model.graph.output)


def prepare_half(stage, side):
  import onnx
  path = stage / (side + '.onnx')
  if side == 'left':
    # TRT 10.3 cannot compute the UINT8 image queue. Retype only the exact
    # byte-valued queue in a scratch copy AFTER resolving the original CPU edges.
    model = onnx.load(path, load_external_data=False)
    patch_stateful_images(model)
    path = stage / 'left-trt.onnx'
    onnx.save_model(model, path)
  return path


def build_half(stage, side, trt, mark, warnings, *, trace=False):
  from openpilot.tools.jetson.nexo_n16_trt_memory import _BoundedBuilder, MIB
  mark('prepare_' + side)
  path = stage / 'left-trace-trt.onnx' if trace else prepare_half(stage, side)

  class Logger(trt.ILogger):
    def log(self, severity, message):
      if severity in (trt.Logger.INTERNAL_ERROR, trt.Logger.ERROR, trt.Logger.WARNING):
        warnings.append(str(message)[:400])

  logger = Logger()
  builder = _BoundedBuilder(trt.Builder(logger), trt, 512 * MIB)
  network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED))
  parser = trt.OnnxParser(network, logger)
  mark('parse_' + side)
  if not parser.parse_from_file(str(path)):
    details = [str(parser.get_error(i))[:500] for i in range(min(parser.num_errors, 4))]
    raise RuntimeError('TensorRT parse: ' + ' | '.join(details))
  config = builder.create_builder_config()
  config.builder_optimization_level = 0
  config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 256 * MIB)
  if config.get_memory_pool_limit(trt.MemoryPoolType.WORKSPACE) != 256 * MIB:
    raise RuntimeError('TensorRT rejected workspace bound')
  config.clear_flag(trt.BuilderFlag.TF32)
  mark('build_' + side)
  serialized = builder.build_serialized_network(network, config)
  if serialized is None:
    raise RuntimeError('TensorRT builder returned no plan: ' + ' | '.join(list(warnings)[-3:]))
  data = memoryview(serialized)
  if not data.nbytes or shutil.disk_usage(stage).free < DISK_FLOOR + data.nbytes:
    raise RuntimeError('Insufficient scratch plan or disk reserve')
  plan = stage / ('left-trace.plan' if trace else side + '.plan')
  with plan.open('xb') as stream:
    stream.write(data)
  mark('built_' + side)
  return {'bytes': data.nbytes, 'sha256': digest(plan), 'strongly_typed': True, 'tf32': False,
          'workspace_mib': 256, 'tactic_dram_mib': 512, 'aux_streams': 0}


class GpuSession:
  """Static ORT-shaped IO contract for the existing strict recurrent comparator."""
  def __init__(self, engine, model):
    from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import ort_dtype
    self.engine = engine
    self.inputs, self.outputs = half_contract(model)
    for expected, actual in ((self.inputs, engine.inputs), (self.outputs, engine.outputs)):
      if (expected.keys() != actual.keys() or any(tuple(x.shape) != actual[name].shape or
          ort_dtype(x.type) != actual[name].dtype for name, x in expected.items())):
        raise ValueError('GPU engine IO differs from staged ONNX: ' + model.name)
    self.times = []

  def get_inputs(self):
    return list(self.inputs.values())

  def get_outputs(self):
    return list(self.outputs.values())

  def run(self, names, feeds):
    import numpy as np
    start = time.perf_counter()
    for name, array in feeds.items():
      np.copyto(self.engine.host_input(name), array, casting='no')
    values = self.engine.run()  # Vendor synchronizes the stream before returning pinned views.
    self.times.append((time.perf_counter() - start) * 1000)
    # Recurrent state must outlive the next write to the engine's pinned buffers.
    return [values[name].copy() for name in (names or self.outputs)]


def replay_gpu(stage, spec, mark):
  import numpy as np
  from jetlink.server.backends.trt.engine import TrtEngine
  with ExitStack() as stack:
    sessions = []
    for side in ('left', 'right'):
      plan = stage / (side + '.plan')
      built = read_report(stage / ('build_' + side + '-result.json'))
      if plan.stat().st_size != built['bytes'] or digest(plan) != built['sha256']:
        raise ValueError('Staged engine identity mismatch: ' + side)
      mark('load_' + side)
      engine = TrtEngine(str(plan))
      stack.callback(engine.close)
      sessions.append(GpuSession(engine, stage / ('left-trt.onnx' if side == 'left' else 'right.onnx')))
    reference = ReferenceReplay(stage / 'reference.xz', spec)
    stack.callback(reference.close)
    boundary = load_arrays(stage / 'first-boundary.npz')
    proof = read_report(stage / 'probe-result.json')['plan']
    if set(boundary) != {e['tensor'] for e in proof['boundary']}:
      raise ValueError('Missing GPU boundary comparison')
    result = compare_sequence(reference, *sessions, spec, on_frame=lambda i: mark('gpu_frame_' + str(i)),
                              first_boundary=boundary)
    reference.finish()
    left, right = (s.times for s in sessions)
    durations = np.asarray(left) + np.asarray(right)
    if len(durations) != FRAMES or not np.isfinite(durations).all() or min(durations) <= 0:
      raise RuntimeError('Incomplete synchronized GPU timings')
    passed = result['stateful_140_frames_verified']
    return {**{k: result[k] for k in ('frames_completed', 'failed_outputs', 'failed_output_count',
             'policy', 'distinct_image_frames', 'max_abs_error_by_output', 'first_frame_boundary_max_abs_error')},
            'state': 'gpu_parity_passed' if passed else 'gpu_parity_failed', 'gpu_parity_verified': passed,
            'gpu_inference_verified': True, 'left_ms': left, 'right_ms': right,
            'timing_scope': 'host_copy_enqueue_transfers_and_stream_sync_both_engines',
            'mean_ms': float(np.mean(durations)), 'p50_ms': float(np.median(durations)),
            'p95_ms': float(np.percentile(durations, 95)), 'max_ms': float(max(durations)),
            'paired_engine_fps': float(1000 / np.mean(durations)),
            'split_frame_ms': result['performance_cpu_only_ms']['split_ms'],
            'cpu_oracle_ms': result['performance_cpu_only_ms']['reference_ms']}


def gpu_child(role, release, stage):
  sys.path.insert(0, str(release / 'carrot-server/third_party/jetlink'))
  os.environ['CUDA_MODULE_LOADING'] = 'LAZY'
  phase = role
  started = time.monotonic()
  warnings = deque(maxlen=8)
  metrics = {}
  path = stage / (role + '-telemetry.json')
  def mark(name):
    nonlocal phase
    phase = name
    snapshot = memory_snapshot()
    if role.startswith('build_'):
      snapshot.update(worker_cpu_s=round(time.process_time(), 3), elapsed_s=round(time.monotonic() - started, 3),
                      worker_nice=builder_nice)
    metrics[name] = snapshot
    save_json(stage / 'phase.json', {'phase': name})
    save_json(path, {'phase': name, 'samples': metrics, 'warnings': list(warnings)})
  try:
    if role.startswith('build_'):
      # Apply before TensorRT imports/creates any native builder threads.
      # Keep the parent SSH/Offroad and memory monitor at its existing priority.
      builder_nice = os.nice(5)
    import tensorrt as trt
    if trt.__version__ != '10.3.0':
      raise ValueError('Requires TensorRT 10.3.0')
    mark(role + '_start')
    if role.startswith('build_'):
      side = 'left' if role == 'build_left_trace' else role.removeprefix('build_')
      result = build_half(stage, side, trt, mark, warnings, trace=role == 'build_left_trace')
      result['worker_nice'] = builder_nice
    elif role in ('gpu_plain', 'gpu_trace'):
      from openpilot.tools.jetson.nexo_n16_gpu_trace import replay_trace
      result = replay_trace(stage, baseline=role == 'gpu_plain')
    else:
      result = replay_gpu(stage, load_registry()[0], mark)
    mark(role + '_complete')
    save_json(stage / (role + '-result.json'), result)
  except Exception as error:
    save_json(path, {'phase': phase, 'samples': metrics, 'warnings': list(warnings)})
    save_json(stage / (role + '-failure.json'), {'error': f'{phase}: {type(error).__name__}: {error}'[:1200]})
    raise


def run_offline(stage, release, source, cpu_report, on_phase, trace_targets=()):
  spec, _ = load_registry()
  approved = accepted_cpu_plan(cpu_report, spec)
  rss = {}
  def run(target, args, role):
    on_phase(role)
    result = isolated_phase(target, args, role, stage, on_phase, sample_path=stage / 'stateful-cpu-rss.json',
                            timeout_s=TRIAL_TIMEOUT_S if role.startswith('build_') else None)
    rss[role] = result['controller_and_cpu_worker_rss_bytes_sampled']
    return result
  for role in ('reference', 'probe'):
    result = run(_cpu_child, (role, source, release, 360, stage, True, trace_targets), role)
    if result['ort_version'] != cpu_report['ort_version']:
      raise ValueError('CPU runtime differs from accepted receipt')
    if role == 'reference' and result['frames'] != (1 if trace_targets else FRAMES):
      raise ValueError('Incomplete CPU oracle')
  if result['plan'] != approved:
    raise ValueError('Regenerated cut-360 boundary or first-frame identity differs from accepted CPU receipt')
  if trace_targets:
    for role in ('trace_original', 'trace_patched'):
      run(_cpu_child, (role, source, release, 360, stage, True, trace_targets), role)
    plain = run(gpu_child, ('build_left', release, stage), 'build_left')
    baseline = run(gpu_child, ('gpu_plain', release, stage), 'gpu_plain')
    (stage / 'left.plan').unlink()  # Only this trial's scratch plan. Keep one engine on DATA at a time.
    traced = run(gpu_child, ('build_left_trace', release, stage), 'build_left_trace')
    result = run(gpu_child, ('gpu_trace', release, stage), 'gpu_trace')
    return {**result, 'engines': {'left': plain, 'left_instrumented': traced},
            'plain_gpu_to_cpu_boundary_errors': baseline['boundary_errors'], 'engine_build_attempted': True,
            'stage_controller_and_worker_rss_bytes_sampled': rss}
  builds = {}
  for side in ('left', 'right'):
    role = 'build_' + side
    builds[side] = run(gpu_child, (role, release, stage), role)
  result = run(gpu_child, ('gpu_replay', release, stage), 'gpu_replay')
  return {**result, 'phase': 'offline_gpu_replay_complete', 'engines': builds, 'cut_node_index': 360,
          'model_sha256': MODEL_SHA, 'stage_controller_and_worker_rss_bytes_sampled': rss,
          'engine_build_attempted': True, 'production_activation_allowed': False}


def memory_summary(telemetry):
  """CUDA observations include other GPU users and unified RAM on Jetson."""
  result = {}
  for role, entry in telemetry.items():
    samples = [s for s in entry.get('samples', {}).values() if s.get('cuda_memory_available')]
    result[role] = {'cuda_samples': len(samples),
                    'min_device_free_bytes': min((s['cuda_free_bytes'] for s in samples), default=None),
                    'max_device_used_bytes_sampled': max((s['cuda_total_bytes'] - s['cuda_free_bytes']
                                                          for s in samples), default=None)}
  return result
