"""Explicit Offroad-only TensorRT comparisons. No production cache publication.

Invoked only by prepare_engine --experiment. Original files and external model
contract survive. Experimental success never enables AI or approves arithmetic.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time

TRIALS = ('native_fp16', 'native_strongly_typed', 'fp32_compute', 'native_no_fp16_flag', 'gelu_fp32_island',
          'region_conv_gelu', 'region_conv', 'region_gelu')


def fp32_compute(model):
  """Independent FP32 arithmetic copy, retaining exact external tensor contracts.

  Disable TF32/FP16 in the builder too. This changes rounding and must never
  enter the production model/cache. Do not equate it to turning FP16 off.
  """
  import onnx
  from onnx import TensorProto as T, helper, numpy_helper
  import numpy as np
  result = copy.deepcopy(model)
  if result.graph.sparse_initializer or result.functions:
    raise ValueError('FP32 comparison requires the checked flat NEXO graph')
  original_io = interfaces(result)
  names = {v for node in result.graph.node for v in (*node.input, *node.output)}
  def unique(name):
    value = name + '__nexo_fp32_compute'
    if value in names:
      raise ValueError('FP32 diagnostic tensor name collision')
    names.add(value)
    return value
  before, after, replacements = [], [], {}
  for value in result.graph.input:
    if value.type.tensor_type.elem_type == T.FLOAT16:
      internal = unique(value.name)
      replacements[value.name] = internal
      before.append(helper.make_node('Cast', [value.name], [internal], to=T.FLOAT))
  for value in result.graph.output:
    if value.type.tensor_type.elem_type == T.FLOAT16:
      internal = unique(value.name)
      replacements[value.name] = internal
      after.append(helper.make_node('Cast', [internal], [value.name], to=T.FLOAT16))
  def convert_tensor(tensor):
    if tensor.data_type == T.FLOAT16:
      tensor.CopyFrom(numpy_helper.from_array(numpy_helper.to_array(tensor).astype(np.float32), tensor.name))
  for tensor in result.graph.initializer:
    convert_tensor(tensor)
  for node in result.graph.node:
    for attr in node.attribute:
      if attr.type in (onnx.AttributeProto.GRAPH, onnx.AttributeProto.GRAPHS):
        raise ValueError('Nested graph not supported by the FP32 experiment')
      if attr.type == onnx.AttributeProto.TENSOR:
        convert_tensor(attr.t)
      elif attr.type == onnx.AttributeProto.TENSORS:
        for tensor in attr.tensors:
          convert_tensor(tensor)
      if node.op_type == 'Cast' and attr.name == 'to' and attr.i == T.FLOAT16:
        attr.i = T.FLOAT
    for field in (node.input, node.output):
      for i, name in enumerate(field):
        field[i] = replacements.get(name, name)
  for value in result.graph.value_info:
    value.name = replacements.get(value.name, value.name)
    if value.type.tensor_type.elem_type == T.FLOAT16:
      value.type.tensor_type.elem_type = T.FLOAT
  nodes = list(result.graph.node)
  del result.graph.node[:]
  result.graph.node.extend([*before, *nodes, *after])
  onnx.checker.check_model(result, full_check=True)
  onnx.shape_inference.infer_shapes(result, check_type=True, strict_mode=True)
  if interfaces(result) != original_io:
    raise ValueError('FP32 diagnostic changed external I/O')
  return result


@contextmanager
def checked_parser_input(trial):
  """Copy a verified, already-adapted export for one isolated build.

  The normal adapter accepts raw UINT8 NEXO only. These exports already
  passed it, or are exact extracted regions; never adapt them twice.
  """
  from jetlink import onnx_patch
  from openpilot.tools.jetson.onnx_inputs import PINNED_PATCH_SHA256
  if hashlib.sha256(Path(onnx_patch.__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != PINNED_PATCH_SHA256:
    raise ValueError('Experiments require the pinned ONNX patch module')
  original = onnx_patch.patch_file
  def copy_checked(source, destination):
    if (Path(source).resolve() != Path(trial['onnx']).resolve()
        or hashlib.sha256(Path(source).read_bytes()).hexdigest() != trial['sha256']):
      raise ValueError('Refusing an unverified experimental parser input')
    shutil.copyfile(source, destination)
  onnx_patch.patch_file = copy_checked
  try:
    yield
  finally:
    onnx_patch.patch_file = original


def interfaces(model):
  import onnx
  def side(values):
    return {v.name: {'shape': [d.dim_value for d in v.type.tensor_type.shape.dim],
                     'dtype': onnx.TensorProto.DataType.Name(v.type.tensor_type.elem_type)} for v in values}
  return {'inputs': side(model.graph.input), 'outputs': side(model.graph.output)}


def export_trials(source, destination):
  """Extract exact regions; arithmetic changes are isolated, explicitly named copies."""
  import onnx
  from onnx.utils import Extractor
  from openpilot.tools.jetson import onnx_inputs, trt_diagnostics as diag
  from openpilot.tools.jetson.trt_conv_gelu_experiment import make_candidate

  source, destination = Path(source), Path(destination)
  facts = diag.analyze_model(source)  # Exact original SHA, full checker and inferred shapes.
  destination.mkdir(exist_ok=False)
  native = onnx.load(source)
  inferred = onnx.shape_inference.infer_shapes(native, check_type=True, strict_mode=True)
  baseline = onnx_inputs.adapt(native)
  island_path = destination / 'island-source.onnx'
  make_candidate(source, island_path)
  island_source = onnx.load(island_path)
  if interfaces(island_source) != interfaces(native):
    raise ValueError('Experimental island changed the external NEXO interface')
  island = onnx_inputs.adapt(island_source)
  region = Extractor(inferred)
  a, b, c = facts['input']['name'], facts['boundary']['name'], facts['output']['name']
  models = [baseline, baseline, fp32_compute(baseline), baseline, island,
            region.extract_model([a], [c]), region.extract_model([a], [b]), region.extract_model([b], [c])]
  result = []
  for name, model in zip(TRIALS, models, strict=True):
    if name == 'native_strongly_typed':
      # Use the very same parser bytes and synthetic inputs as the native build.
      result.append({**result[0], 'name': name, 'fp16_flag': False})
      continue
    if name.startswith('region_'):
      # A subgraph is never a driving model, even if accidentally copied.
      del model.metadata_props[:]
    onnx.checker.check_model(model, full_check=True)
    path = destination / (name + '.onnx')
    onnx.save(model, path)
    result.append({'name': name, 'onnx': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'fp16_flag': name not in ('native_no_fp16_flag', 'fp32_compute'),
                   'strict_fp32': name == 'fp32_compute', 'interface': interfaces(model),
                   'full_model': not name.startswith('region_'),
                   'arithmetic_rewrite': name in ('gelu_fp32_island', 'fp32_compute')})
  return result


@contextmanager
def trial_network_typing(builder, trt, name):
  if name != 'native_strongly_typed':
    yield
    return
  if getattr(trt.NetworkDefinitionCreationFlag, 'STRONGLY_TYPED', None) is None:
    raise ValueError('This TensorRT runtime does not expose STRONGLY_TYPED')
  # Select the pinned Carrot builder's existing typed network, precision and
  # metadata branches for this single build in the isolated experiment worker.
  original = builder.strongly_typed_only
  builder.strongly_typed_only = lambda: True
  try:
    yield
  finally:
    builder.strongly_typed_only = original


def memory_snapshot():
  """CUDA free/total is a sample, never a claim of peak free GPU memory."""
  result = {}
  try:
    for line in Path('/proc/meminfo').read_text().splitlines():
      key, _, value = line.partition(':')
      if key in ('MemAvailable', 'MemTotal', 'SwapFree'):
        result[key + '_bytes'] = int(value.split()[0]) * 1024
  except (OSError, ValueError):
    pass
  try:
    from jetlink.server.backends.trt import cudart
    free, total = cudart.check(cudart._rt.cudaMemGetInfo())
    result.update(cuda_free_bytes=int(free), cuda_total_bytes=int(total), cuda_memory_available=True)
  except Exception as error:
    result.update(cuda_memory_available=False, cuda_memory_error=f'{type(error).__name__}: {error}'[:240])
  return result


@contextmanager
def build_memory(path=None):
  """Bounded host/unified-memory low water mark; no extra CUDA thread/context."""
  stop = threading.Event()
  result = {'samples': 0, 'host_memavailable_min_bytes': None, 'scope': 'host/unified MemAvailable, not CUDA peak'}
  def sample():
    try:
      line = next(v for v in Path('/proc/meminfo').read_text().splitlines() if v.startswith('MemAvailable:'))
      value = int(line.split()[1]) * 1024
      result['samples'] += 1
      previous = result['host_memavailable_min_bytes']
      result['host_memavailable_min_bytes'] = value if previous is None else min(previous, value)
      if path is not None and (previous is None or value < previous):
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result))
        temporary.replace(path)
    except (OSError, ValueError, StopIteration):
      pass
  def observe():
    while not stop.wait(.25):
      sample()
  sample()
  thread = threading.Thread(target=observe, name='nexo-build-memory', daemon=True)
  thread.start()
  try:
    yield result
  finally:
    stop.set()
    thread.join(timeout=1)
    sample()


def compare_outputs(observed, expected):
  import numpy as np
  result = {}
  for name, value in observed.items():
    reference = expected[name]
    delta = value.astype(np.float64) - reference.astype(np.float64)
    result[name] = {'max_abs_error': float(np.max(np.abs(delta))),
                    'rmse': float(np.sqrt(np.mean(delta**2))),
                    'equal_elements': int(np.count_nonzero(value == reference)), 'elements': int(value.size)}
  return result


def run_trial(trial, directory, samples, *, builder, engine_type, trt, native_reference=None, workspace=None):
  import numpy as np
  import onnx
  from onnx.reference import ReferenceEvaluator
  from openpilot.tools.jetson.onnx_contract import benchmark_engine, validate_outputs
  from openpilot.tools.jetson.trt_diagnostics import instrument_builder, MODEL_SHA
  from openpilot.tools.jetson.trt_observation import verify_cleanup

  directory.mkdir(exist_ok=False)
  plan = directory / 'experiment.plan'
  row = {**trial, 'state': 'building', 'production_cache_published': False,
         'numerical_equivalence_verified': False, 'vehicle_approved': False,
         'memory_before': memory_snapshot(), 'build_log': str(plan.with_suffix('.build.json'))}
  def analysis(path):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != trial['sha256']:
      raise ValueError('Experimental ONNX changed before build')
    return {'source_model_sha256': MODEL_SHA, 'candidate_sha256': trial['sha256'],
            'arithmetic_rewrite': trial['arithmetic_rewrite'], 'full_model': trial['full_model'],
            'gpu_verified': False, 'vehicle_approved': False}
  instrument_builder(builder, trt, analysis=analysis, profile='nexo-experiment-' + trial['name'],
                     verbose_log=True, strict_fp32=trial.get('strict_fp32', False))
  engine, outputs = None, None
  started = time.monotonic()
  try:
    with (build_memory(directory / 'build-memory.json') as low_water, checked_parser_input(trial),
          trial_network_typing(builder, trt, trial['name'])):
      builder.build_engine(trial['onnx'], plan, fp16=trial['fp16_flag'], timing_cache=None, workspace=workspace,
                           meta_extra={'experiment_only': True, 'source_model_sha256': MODEL_SHA})
    row.update(build_elapsed_s=round(time.monotonic() - started, 3), build_memory=low_water,
               memory_after_build=memory_snapshot(), plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
               state='loading')
    engine = engine_type(str(plan))
    expected_io = trial['interface']
    for side, bindings in (('inputs', engine.inputs), ('outputs', engine.outputs)):
      if set(bindings) != set(expected_io[side]):
        raise ValueError('Experimental TensorRT tensor names differ from ONNX')
      for name, binding in bindings.items():
        item = expected_io[side][name]
        if (list(binding.shape) != item['shape']
            or np.dtype(binding.dtype) != np.dtype(onnx.helper.tensor_dtype_to_np_dtype(getattr(onnx.TensorProto, item['dtype'])))):
          raise ValueError('Experimental TensorRT shape/dtype differs from ONNX')
    rng = np.random.default_rng(20261115)
    feeds = {}
    for name in sorted(engine.inputs):
      binding = engine.inputs[name]
      values = (rng.integers(0, 256, binding.shape) if name in ('img', 'big_img') else
                rng.uniform(-2, 2, binding.shape) if not trial['full_model'] else np.zeros(binding.shape))
      feeds[name] = np.asarray(values, dtype=binding.dtype)
    def prepare(index):
      for name, value in feeds.items():
        np.copyto(engine.host_input(name), value)
    prepare(0)
    row['warm_mode'] = engine.warm()
    timing = benchmark_engine(engine, prepare, {n: tuple(v['shape']) for n, v in expected_io['outputs'].items()},
                              {n: b.dtype for n, b in engine.outputs.items()}, samples=samples)
    outputs = {n: v.copy() for n, v in engine.run().items()}
    validate_outputs(outputs, {n: tuple(v['shape']) for n, v in expected_io['outputs'].items()},
                     {n: b.dtype for n, b in engine.outputs.items()})
    reference = native_reference if trial['full_model'] else dict(zip(engine.outputs,
      ReferenceEvaluator(trial['onnx']).run(None, feeds), strict=True))
    row.update(state='gpu_synthetic_passed', **timing, cuda_graph_active=engine.graph_exec is not None,
               synthetic_fps=(1000 / timing['synchronized_inference_ms']['mean']
                              if timing['synchronized_inference_ms']['mean'] > 0 else None),
               reference_scope='native GPU same synthetic input' if trial['full_model'] else 'ONNX CPU extracted region')
    if reference is not None:
      row['output_comparison'] = compare_outputs(outputs, reference)
      if trial['full_model']:
        # Exact original SHA's lead/plan slices; diagnostic comparison only.
        for label, start, end in (('lead', 917, 1061), ('plan', 1576, 2566)):
          row[label + '_comparison'] = compare_outputs({'outputs': outputs['outputs'][:, start:end]},
                                                       {'outputs': reference['outputs'][:, start:end]})
    else:
      row['reference_unavailable'] = 'Native GPU baseline did not succeed; full-model numeric comparison unverified'
  except Exception as error:
    row.update(state='failed', error=f'{type(error).__name__}: {error}'[:1600])
  finally:
    if engine is not None:
      try:
        engine.close()
        verify_cleanup(engine)
      except Exception as error:
        row.update(state='failed', cleanup_error=str(error)[:600])
    row['memory_after'] = memory_snapshot()
    row.setdefault('build_memory', locals().get('low_water', {}))
    if plan.with_suffix('.build.json').is_file():
      detail = json.loads(plan.with_suffix('.build.json').read_text())
      row['failure_diagnosis'] = detail.get('failure_diagnosis')
      row['workspace_bytes'] = detail.get('workspace_bytes')
      row.update({k: detail.get(k) for k in ('verbose_log_path', 'full_log_truncated', 'full_log_error')})
    (directory / 'result.json').write_text(json.dumps(row, indent=2, allow_nan=False))
  return row, outputs


def worker(root, release, stage, samples=100):
  """The parent's existing Offroad/timeout/configuration guards cover every trial."""
  import os
  import sys
  from openpilot.tools.jetson.prepare_engine import MODEL, store, runtime_preflight, digest
  from openpilot.tools.jetson.trt_diagnostics import MODEL_SHA
  result = {'state': 'running', 'phase': 'experiment_matrix', 'model_sha256': MODEL_SHA,
            'production_cache_published': False, 'driving_activation_performed': False,
            'vehicle_inference_verified': False, 'native_gpu_verified': False, 'trials': []}
  os.setsid()
  report = stage / 'result.json'
  store(report, result)
  try:
    sys.path[:0] = [str(release), str(release / 'carrot-server/third_party/jetlink')]
    import tensorrt as trt
    import platform
    from jetlink.server.backends.trt import build, engine
    from openpilot.tools.jetson.trt_observation import engine_class
    from openpilot.tools.jetson.onnx_inputs import install_carrot_patch
    runtime_preflight(json.loads((release / 'NEXO_RUNTIME.json').read_text()), sys.version_info[:2], platform.machine(), trt.__version__)
    result.update(trt_version=trt.__version__, device=build.device_tag(),
                  l4t_release=Path('/etc/nv_tegra_release').read_text()[:240])
    if digest(release / MODEL) != MODEL_SHA:
      raise ValueError('Experimental worker requires the original NEXO model')
    install_carrot_patch()
    trials = export_trials(release / MODEL, stage / 'models')
    checked = engine_class(engine)
    baseline = None
    workspace = build.workspace_bytes()  # Same upstream policy, one fixed budget for the comparison.
    result['matrix_workspace_bytes'] = workspace
    for trial in trials:
      result['current_trial'] = trial['name']
      store(report, result)
      row, values = run_trial(trial, stage / trial['name'], samples, builder=build, engine_type=checked, trt=trt,
                              native_reference=baseline, workspace=workspace)
      result['trials'].append(row)
      if trial['name'] == 'native_fp16' and row['state'] == 'gpu_synthetic_passed':
        baseline = values
        result['native_gpu_verified'] = True
      store(report, result)
    result['state'] = 'passed'  # Matrix collection only; parent never publishes experimental plans.
  except BaseException as error:
    result.update(state='failed', error=f'{type(error).__name__}: {error}'[:1600])
  finally:
    store(report, result)
