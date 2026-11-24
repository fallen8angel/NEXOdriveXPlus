"""Opt-in, guarded OFFROAD-only Carrot N16 TensorRT experiment (2019 NEXO).

Uses the pinned Carrot builder without modifying its installed files. Supplies
the stateful-image UINT8->FP16 ONNX patch from the newer Carrot implementation
IN THIS WORKER PROCESS ONLY. The N16 source, live modeld, CAN, HUD, engine
cache, last-loaded.json, services and release selection are never changed.

A successful synthetic inference is *not* vehicle accuracy or driving approval.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
from contextlib import nullcontext
import shutil
import signal
import sys
import tempfile
import time

from openpilot.tools.jetson.nexo_n16_diagnostics import (
  TIMELINE, Timeline, assess_feasibility, classify_trial, hud_observer_contract, read_report,
  save_json, system_sample,
)

ROOT = Path('/opt/carrot-jetlink')
N16_SOURCE = ROOT / 'cache/nexo-n16-download/carrot-n16.onnx'
MODEL_SHA = '01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a'
MODEL_BYTES = 814591107
DISK_MIN = 4 * 1024 ** 3
# Original ~815 MB ONNX is staged once for CPU-only parity, then discarded.
# Retain the existing 1.5 GiB runtime disk floor throughout the trial.
CPU_PARITY_DISK_MIN = 3 * 1024 ** 3
DISK_FLOOR = 1536 * 1024 ** 2
MEMORY_MIN = 5 * 1024 ** 3
MEMORY_FLOOR = 2 * 1024 ** 3
SAMPLES_MAX = 10
# Wall-clock budgets for the entire worker, including model/session preparation.
TRIAL_TIMEOUT_S, MAX_TRIAL_TIMEOUT_S = 900, 1200
STATEFUL_CPU_TIMEOUT_S, MAX_STATEFUL_CPU_TIMEOUT_S = 3600, 7200


class TrialTimeLimit(TimeoutError):
  """The bounded trial expired, independently of SSH and worker errors."""


def trial_time_limit(requested: int | None, stateful_cpu: bool) -> int:
  default, maximum = ((STATEFUL_CPU_TIMEOUT_S, MAX_STATEFUL_CPU_TIMEOUT_S) if stateful_cpu
                      else (TRIAL_TIMEOUT_S, MAX_TRIAL_TIMEOUT_S))
  if requested is None:
    return default
  if type(requested) is not int or not 60 <= requested <= maximum:
    raise ValueError(f'Time budget must be an integer between 60 and {maximum} seconds')
  return requested


def cpu_frame_progress(phase: str) -> dict:
  from openpilot.tools.jetson.nexo_n16_cpu_precision import FRAMES
  for prefix, stage in (('stateful_cpu_reference_frame_', 'reference'), ('stateful_cpu_frame_', 'split'),
                        ('gpu_frame_', 'split_gpu')):
    index = phase.removeprefix(prefix)
    if index != phase and index.isdecimal() and int(index) < FRAMES:
      # The worker emits its zero-based marker BEFORE computing the frame.
      return {'stage': stage, 'current_frame': int(index) + 1, 'frames_per_stage': FRAMES}
  return {'stage': None, 'current_frame': None, 'frames_per_stage': FRAMES}


def validate_build_profile(workspace_mib: int | None, optimization_level: int) -> int | None:
  """Only bounded experiment settings; never modify the installed builder defaults."""
  if workspace_mib is not None and (type(workspace_mib) is not int or not 256 <= workspace_mib <= 1024):
    raise ValueError('workspace_mib must be between 256 and 1024 MiB')
  if type(optimization_level) is not int or not 0 <= optimization_level <= 3:
    raise ValueError('optimization_level must be 0..3')
  return workspace_mib * (1024 ** 2) if workspace_mib is not None else None


def digest(path: Path) -> str:
  value = hashlib.sha256()
  with path.open('rb') as source:
    for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
      value.update(block)
  return value.hexdigest()


def mem_available() -> int:
  for row in Path('/proc/meminfo').read_text().splitlines():
    if row.startswith('MemAvailable:'):
      return int(row.split()[1]) * 1024
  return 0


def patch_stateful_images(model):
  """Port of Carrot's stateful-image patch; no new model/weights or arithmetic.

  Source: ajouatom/openpilot carrot-wip
  third_party/jetlink/jetlink/onnx_patch.py:patch_stateful_images.
  Byte-preserving queue operations are exactly representable in FP16 (0..255).
  """
  from onnx import TensorProto, helper

  graph = model.graph
  inputs = {value.name: value for value in graph.input}
  promoted = {'new_img', 'state_img_q'}
  if any(name not in inputs or inputs[name].type.tensor_type.elem_type != TensorProto.UINT8
         for name in promoted):
    raise ValueError('Expected UINT8 new_img and state_img_q')
  for node in graph.node:
    affected = [name for name in node.input if name in promoted]
    if not affected:
      continue
    if node.domain:
      raise ValueError('Unknown image queue domain: ' + node.domain)
    if node.op_type == 'Cast':
      to = next((helper.get_attribute_value(attr) for attr in node.attribute if attr.name == 'to'), None)
      if to not in (TensorProto.FLOAT16, TensorProto.FLOAT):
        raise ValueError('Image queue Cast must target floating point')
      continue
    if node.op_type == 'Concat':
      valid = all(name in promoted for name in node.input)
    else:
      valid = (node.op_type in ('Slice', 'Unsqueeze', 'Squeeze', 'Gather', 'Reshape',
                                'Transpose', 'Identity') and node.input[0] in promoted
               and len(affected) == 1)
    if not valid:
      raise ValueError('Unsupported image queue operation: ' + node.op_type)
    promoted.update(node.output)
  if 'next_state_img_q' not in promoted:
    raise ValueError('Image queue output is not derived from image inputs')
  for value in [*graph.input, *graph.value_info, *graph.output]:
    if value.name in promoted:
      if value.type.tensor_type.elem_type != TensorProto.UINT8:
        raise ValueError('Image queue tensor is not UINT8: ' + value.name)
  for value in [*graph.input, *graph.value_info, *graph.output]:
    if value.name in promoted:
      value.type.tensor_type.elem_type = TensorProto.FLOAT16
  return model


def _vendor(release: Path, prepatched: Path | None = None):
  sys.path[:0] = [str(release), str(release / 'carrot-server/third_party/jetlink')]
  from jetlink import onnx_patch
  from jetlink.server.backends.trt import build

  if not (hasattr(onnx_patch, 'patch_file') and hasattr(onnx_patch, 'patch_uint8_inputs')
          and hasattr(onnx_patch, 'needs_patch') and hasattr(build, 'build_engine')):
    raise RuntimeError('Pinned Carrot TensorRT patch/builder contract changed')
  source = inspect.getsource(onnx_patch.patch_file)
  if 'patch_uint8_inputs(model)' not in source:
    raise RuntimeError('Carrot patch_file dispatch contract changed; refusing monkeypatch')
  # Change the function in this isolated subprocess ONLY, not the live install.
  onnx_patch.IMG_INPUTS = tuple(dict.fromkeys((*onnx_patch.IMG_INPUTS, 'new_img', 'state_img_q')))
  onnx_patch.patch_uint8_inputs = patch_stateful_images
  if prepatched is not None:
    # The ONNX parser's 800+ MB Python object was released by a short-lived
    # CPU-only subprocess. The builder gets the checked file by hardlink rather
    # than loading/rewriting the graph in this already memory-heavy process.
    if prepatched.is_symlink() or not prepatched.is_file() or prepatched.stat().st_size < 100_000_000:
      raise RuntimeError('N16 staged ONNX patch missing or invalid')
    original_patch_file = onnx_patch.patch_file
    def link_prepatched(src, dst, check=True):
      if Path(src).resolve() != N16_SOURCE.resolve():
        raise RuntimeError('N16 patch source changed during split builder')
      if Path(dst).exists() or Path(dst).is_symlink():
        raise RuntimeError('N16 patch destination already exists')
      os.link(prepatched, dst)
      return str(dst)
    onnx_patch.patch_file = link_prepatched
  return build


def prepare_onnx_separately(source: Path, output: Path, strip_tinygrad_ops, on_phase=None) -> None:
  """Trusted ONNX graph rewrite in a separate memory-reclaimable process."""
  import onnx
  if source.is_symlink() or not source.is_file():
    raise ValueError('N16 source file must be a regular non-symlink')
  mark = on_phase if on_phase is not None else (lambda *_: None)
  mark('onnx_load')
  model = onnx.load(str(source))
  mark('onnx_retype')
  strip_tinygrad_ops(model)
  patch_stateful_images(model)
  mark('onnx_check')
  onnx.checker.check_model(model, full_check=False)
  mark('onnx_save')
  onnx.save(model, str(output))
  mark('onnx_prepatch_complete')


def _prepatch_worker(release: Path, stage: Path):
  """CPU-only ONNX rewrite. Exits before TensorRT is imported in build worker."""
  output = stage / 'n16-prepatched.onnx'
  try:
    sys.path.insert(0, str(release / 'carrot-server/third_party/jetlink'))
    from jetlink.onnx_patch import strip_tinygrad_ops
    prepare_onnx_separately(
      N16_SOURCE, output, strip_tinygrad_ops,
      on_phase=lambda phase: save_json(stage / 'prepatch-phase.json', {'phase': phase}))
    if output.is_symlink() or not 100_000_000 <= output.stat().st_size <= 2_000_000_000:
      raise ValueError('Prepatched ONNX file size outside bounded trial profile')
    save_json(stage / 'prepatch-status.json', {'ok': True, 'bytes': output.stat().st_size,
                                               'source_model_sha256': MODEL_SHA})
  except BaseException as error:
    save_json(stage / 'prepatch-status.json', {'ok': False,
                                               'error': f'{type(error).__name__}: {error}'[:500]})
    raise



def _store(path: Path, value: dict):
  temporary = path.with_suffix('.tmp')
  temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
  os.replace(temporary, path)


def _worker(release: Path, stage: Path, samples: int, workspace: int | None,
            optimization_level: int, split_builder: bool, tactic_dram_mib: int | None,
            partition_plan_only: bool = False, partition_extract_only: bool = False,
            partition_parity_only: bool = False, partition_boundary_diagnose_only: bool = False,
            partition_cut: int = 364, partition_stateful_cpu_only: bool = False, partition_gpu_parity: bool = False):
  os.setsid()
  if tactic_dram_mib is not None:
    os.environ.setdefault('CUDA_MODULE_LOADING', 'LAZY')
  status = {'state': 'running', 'phase': 'imports', 'gpu_inference_verified': False,
            'vehicle_control_enabled': False, 'hud_prediction_enabled': False,
            'partition_cut': partition_cut, 'production_engine_published': False}
  engine = None
  prepatched = None
  try:
    from openpilot.tools.jetson.nexo_n16_registry import load_registry
  except BaseException as error:
    status.update(state='failed', error=f'{type(error).__name__}: {error}'[:600])
    _store(stage / 'result.json', status)
    return
  try:
    spec, _ = load_registry()
    if spec['sha256'] != MODEL_SHA or spec['nbytes'] != MODEL_BYTES:
      raise ValueError('Pinned N16 contract mismatch')
    if digest(N16_SOURCE) != MODEL_SHA:
      raise ValueError('N16 source SHA mismatch before build')
    if partition_gpu_parity:
      from openpilot.tools.jetson.nexo_n16_split_gpu import run_offline
      def gpu_phase(name):
        status['phase'] = name
        _store(stage / 'result.json', status)
      status.update(run_offline(stage, release, N16_SOURCE,
                                read_report(ROOT / 'cache/nexo-n16-stateful-cpu-parity-result.json'), gpu_phase))
      return
    if partition_stateful_cpu_only:
      from openpilot.tools.jetson.nexo_n16_cpu_precision import run_stateful_cpu
      status.update(engine_build_attempted=False, gpu_inference_verified=False)
      def stateful_phase(phase):
        status['phase'] = phase
        status['cpu_memory_stages'] = read_report(stage / 'stateful-cpu-memory.json')
        _store(stage / 'result.json', status)
      status.update(run_stateful_cpu(N16_SOURCE, release, partition_cut, stateful_phase, stage=stage))
      return
    if partition_plan_only:
      # CPU-only graph boundary analysis. Never launch TensorRT or GPU.
      status.update(phase='onnx_partition_plan', engine_build_attempted=False)
      _store(stage / 'result.json', status)
      from openpilot.tools.jetson.nexo_n16_partition_plan import inspect_pinned_model
      status.update(inspect_pinned_model(N16_SOURCE, release))
      status['phase'] = 'onnx_partition_plan'
      status['gpu_inference_verified'] = False
      status['production_engine_published'] = False
      return
    if partition_extract_only:
      # Keep graph extraction CPU-only, with one ONNX load per child process.
      # The protected parent may publish only after its final Offroad check.
      from openpilot.tools.jetson.nexo_n16_partition_extract import (
        ALLOWED_N16_CUTS, half_worker, verify_exported_halves,
      )
      if type(partition_cut) is not int or partition_cut not in ALLOWED_N16_CUTS:
        raise ValueError('Unsupported N16 partition cut')
      output_dir = stage / 'partition-export'
      output_dir.mkdir(mode=0o700)
      for side in ('left', 'right'):
        status.update(phase='onnx_extract_' + side, engine_build_attempted=False,
                      partition_extract_only=True)
        _store(stage / 'result.json', status)
        job = multiprocessing.get_context('spawn').Process(
          target=half_worker,
          args=(N16_SOURCE, release, output_dir, side, MODEL_SHA, partition_cut))
        job.start()
        job.join()
        if job.exitcode != 0:
          raise RuntimeError('N16 ' + side + ' ONNX extraction failed; '
                             'temporary artifacts will be deleted')
      status.update(phase='onnx_partition_verify')
      _store(stage / 'result.json', status)
      status.update(verify_exported_halves(output_dir, partition_cut))
      status['phase'] = 'onnx_partition_extract'
      return
    if partition_parity_only:
      # Three separate ONNX Runtime CPU sessions (original, left, right).
      # The guardian parent watches this worker and all of its children.
      from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import run_cpu_parity
      status.update(phase='n16_cpu_parity_preflight', partition_parity_only=True,
                    engine_build_attempted=False, gpu_inference_verified=False)
      _store(stage / 'result.json', status)
      def on_cpu_phase(phase):
        status['phase'] = phase
        _store(stage / 'result.json', status)
      status.update(run_cpu_parity(release, stage, on_cpu_phase, cut=partition_cut))
      return
    if partition_boundary_diagnose_only:
      # CPU-only diagnostic; external AI / driving / production engine off.
      from openpilot.tools.jetson.nexo_n16_partition_boundary_diagnose import run_boundary_diagnosis
      status.update(phase='n16_boundary_diagnosis_preflight',
                    partition_boundary_diagnose_only=True,
                    engine_build_attempted=False, gpu_inference_verified=False)
      _store(stage / 'result.json', status)
      def boundary_phase(phase):
        status['phase'] = phase
        _store(stage / 'result.json', status)
      status.update(run_boundary_diagnosis(
        release, stage, boundary_phase, cut=partition_cut))
      return
    if split_builder:
      status.update(phase='onnx_patch_separate_process', split_builder=True)
      _store(stage / 'result.json', status)
      subprocess_worker = multiprocessing.get_context('spawn').Process(
        target=_prepatch_worker, args=(release, stage))
      subprocess_worker.start()
      subprocess_worker.join()
      if subprocess_worker.exitcode != 0:
        checked = read_report(stage / 'prepatch-status.json')
        raise RuntimeError('N16 isolated ONNX patch failed: ' +
                           str(checked.get('error', 'missing result'))[:300])
      prepatched = stage / 'n16-prepatched.onnx'
      if not read_report(stage / 'prepatch-status.json').get('ok'):
        raise RuntimeError('N16 isolated ONNX patch did not confirm completion')
    # Import GPU/TensorRT only after the large ONNX model object is released.
    import numpy as np
    import tensorrt as trt
    if trt.__version__ != '10.3.0':
      raise ValueError('Unverified TensorRT version')
    build = _vendor(release, prepatched=prepatched)
    plan = stage / 'n16-trial.plan'
    status.update(phase='tensorrt_build', optimization_level=optimization_level,
                  split_builder=split_builder,
                  tactic_dram_mib=tactic_dram_mib,
                  cuda_module_loading=os.environ.get('CUDA_MODULE_LOADING', 'unset'),
                  workspace_mib=(workspace // (1024 ** 2) if workspace is not None else 'vendor_default'))
    _store(stage / 'result.json', status)
    previous_progress = {'phase': None, 'fraction': -1.0}

    def build_progress(phase, fraction, detail):
      if phase not in ('patch', 'parse', 'build'):
        return
      mapped = {'patch': 'tensorrt_patch', 'parse': 'tensorrt_parse',
                'build': 'tensorrt_engine_build'}[phase]
      # Avoid frequent disk writes from the TensorRT progress callback.
      fraction = max(0.0, min(float(fraction), 1.0))
      if mapped != previous_progress['phase'] or fraction - previous_progress['fraction'] >= 0.1:
        previous_progress.update(phase=mapped, fraction=fraction)
        status.update(phase=mapped, progress=round(fraction, 3))
        _store(stage / 'result.json', status)

    # Preserve pinned Carrot builder and only constrain this isolated trial.
    from openpilot.tools.jetson.nexo_n16_trt_memory import bounded_vendor_builder
    bounds = (bounded_vendor_builder(build, tactic_dram_mib)
              if tactic_dram_mib is not None else nullcontext())
    with bounds:
      build.build_engine(N16_SOURCE, plan, report=build_progress,
                         fp16=True, optimization_level=optimization_level,
                         workspace=workspace,
                         meta_extra={'experimental': True, 'vehicle_control_enabled': False,
                                     'bounded_memory_trial': workspace is not None,
                                     'tactic_dram_mib': tactic_dram_mib,
                                     'single_builder_thread': tactic_dram_mib is not None,
                                     'single_aux_stream': tactic_dram_mib is not None})
    from jetlink.server.backends.trt.engine import TrtEngine
    engine = TrtEngine(str(plan))
    input_shapes = engine.input_shapes
    output_shapes = engine.output_shapes
    if set(input_shapes) != set(spec['input_shapes']) or set(output_shapes) != set(spec['output_shapes']):
      raise ValueError('N16 IO names mismatch')
    for name, shape in spec['input_shapes'].items():
      if list(input_shapes[name]) != shape:
        raise ValueError('N16 input shape mismatch: ' + name)
    for name, shape in spec['output_shapes'].items():
      if list(output_shapes[name]) != shape:
        raise ValueError('N16 output shape mismatch: ' + name)
    for name in input_shapes:
      inp = engine.host_input(name)
      inp.fill(0)
      if name == 'new_img':
        inp.fill(127)
    status['phase'] = 'synthetic_inference'
    _store(stage / 'result.json', status)
    durations = []
    for _ in range(samples):
      started = time.perf_counter()
      output = engine.run()
      durations.append(round((time.perf_counter() - started) * 1000, 2))
      if any(not np.isfinite(item).all() for item in output.values()):
        raise ValueError('N16 synthetic inference produced nonfinite outputs')
    from openpilot.tools.jetson.nexo_n16_hud_observer import summarize_raw_model_output
    hud_offline_preview = summarize_raw_model_output(
      output['outputs'].reshape(-1), source='synthetic')
    status.update(state='passed', phase='synthetic_inference',
                  hud_offline_preview=hud_offline_preview,
                  gpu_inference_verified=True, samples=samples,
                  inference_ms=durations, engine_sha256=digest(plan),
                  input_count=len(input_shapes), output_count=len(output_shapes),
                  input_dtypes={k: str(engine.inputs[k].dtype) for k in input_shapes},
                  model_sha256=MODEL_SHA, note='Synthetic zeros only. No real NEXO camera or numerical accuracy comparison.')
  except BaseException as error:
    status.update(state='failed', error=f'{type(error).__name__}: {error}'[:1000])
  finally:
    if engine is not None:
      try:
        engine.close()
      except Exception as error:
        status.update(state='failed', cleanup_error=type(error).__name__)
    _store(stage / 'result.json', status)


def readiness(guard, root: Path = ROOT, *, disk_min: int = DISK_MIN):
  if not Path('/etc/nv_tegra_release').is_file():
    raise RuntimeError('Requires a real Jetson')
  if guard() is not True:
    raise RuntimeError('Comma4 verified Offroad=1, Onroad=0 and external AI OFF are required')
  release = (root / 'current').resolve(strict=True)
  configuration = (root / 'updates/nexo-ncm.json').read_bytes()
  installed = json.loads(configuration)
  if installed.get('display_only') is not True or installed.get('transport') != 'ncm-tcp':
    raise RuntimeError('Display-only USB-NCM installation required')
  source = root / 'cache/nexo-n16-download/carrot-n16.onnx'
  if source.is_symlink() or not source.is_file() or source.stat().st_size != MODEL_BYTES:
    raise RuntimeError('Verified N16 original is missing')
  if digest(source) != MODEL_SHA:
    raise RuntimeError('N16 source SHA does not match official Carrot model')
  cache = root / 'cache'
  free = shutil.disk_usage(cache).free
  memory = mem_available()
  if free < disk_min or memory < MEMORY_MIN:
    raise RuntimeError(f'Insufficient build reserves: free_disk={free}, MemAvailable={memory}. '
                       'Do not delete protected caches or rollback files.')
  return release, configuration, free, memory


def trial(guard, samples: int = 3, timeout_s: int | None = None,
          workspace_mib: int | None = None, optimization_level: int = 3,
          split_builder: bool = False, tactic_dram_mib: int | None = None,
          partition_plan_only: bool = False, partition_extract_only: bool = False,
          partition_parity_only: bool = False, partition_boundary_diagnose_only: bool = False,
          partition_cut: int = 364, partition_stateful_cpu_only: bool = False, partition_gpu_parity: bool = False):
  sequence_trial = partition_stateful_cpu_only or partition_gpu_parity
  timeout_s = trial_time_limit(timeout_s, sequence_trial)
  if partition_gpu_parity and partition_cut != 360:
    raise ValueError('Offline split GPU parity requires verified cut 360')
  if not 2 <= samples <= SAMPLES_MAX:
    raise ValueError('Invalid sample count')
  workspace = validate_build_profile(workspace_mib, optimization_level)
  from openpilot.tools.jetson.nexo_n16_partition_extract import (
    ALLOWED_N16_CUTS, N16_CUT, partition_export_path,
  )
  if type(partition_cut) is not int or partition_cut not in ALLOWED_N16_CUTS:
    raise ValueError('Unsupported N16 partition cut')
  if partition_cut != N16_CUT and not (
      partition_extract_only or partition_parity_only or partition_boundary_diagnose_only or sequence_trial):
    raise ValueError('Alternate N16 cuts are restricted to CPU extraction/parity/diagnosis modes')
  from openpilot.tools.jetson.nexo_n16_trt_memory import validate_tactic_dram_mib
  validate_tactic_dram_mib(tactic_dram_mib)
  if any(type(x) is not bool for x in (split_builder, partition_plan_only,
                                      partition_extract_only, partition_parity_only,
                                      partition_boundary_diagnose_only, partition_stateful_cpu_only, partition_gpu_parity)):
    raise ValueError('N16 experiment mode flags must be booleans')
  if sum((partition_plan_only, partition_extract_only, partition_parity_only,
          partition_boundary_diagnose_only, partition_stateful_cpu_only, partition_gpu_parity)) > 1:
    raise ValueError('Choose only one partition mode')
  if (partition_plan_only or partition_extract_only or partition_parity_only
      or partition_boundary_diagnose_only or sequence_trial) and (split_builder or workspace_mib is not None
                              or optimization_level != 3 or tactic_dram_mib is not None):
    raise ValueError('Partition modes must not override TensorRT build options')
  release, config, free, memory = readiness(
    guard, disk_min=CPU_PARITY_DISK_MIN if (partition_parity_only or partition_boundary_diagnose_only or partition_stateful_cpu_only) else DISK_MIN)
  cache = ROOT / 'cache'
  lockpath = cache / 'nexo-engine-preparation.lock'
  if lockpath.is_symlink():
    raise RuntimeError('Unsafe engine lock')
  with lockpath.open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stage = Path(tempfile.mkdtemp(prefix='nexo-n16-trial-', dir=cache))
    child = multiprocessing.get_context('spawn').Process(
      target=_worker, args=(release, stage, samples, workspace, optimization_level, split_builder,
                            tactic_dram_mib, partition_plan_only, partition_extract_only,
                            partition_parity_only, partition_boundary_diagnose_only,
                            partition_cut, partition_stateful_cpu_only, partition_gpu_parity))
    timeline = Timeline()
    report = {'state': 'running', 'phase': 'preflight', 'model_sha256': MODEL_SHA,
              'available_disk': free, 'available_memory': memory,
              'workspace_mib': workspace_mib if workspace_mib is not None else 'vendor_default',
              'optimization_level': optimization_level, 'split_builder': split_builder,
              'tactic_dram_mib': tactic_dram_mib, 'partition_plan_only': partition_plan_only,
              'partition_extract_only': partition_extract_only,
              'partition_parity_only': partition_parity_only,
              'partition_boundary_diagnose_only': partition_boundary_diagnose_only,
              'partition_stateful_cpu_only': partition_stateful_cpu_only, 'partition_gpu_parity': partition_gpu_parity,
              'partition_cut': partition_cut,
              'minimum_memory_available_bytes': memory,
              'vehicle_control_enabled': False, 'production_engine_published': False}
    if partition_gpu_parity:
      report.update(workspace_mib=256, tactic_dram_mib=512, optimization_level=0, gpu_inference_verified=False,
                    gpu_parity_verified=False, engine_build_attempted=False, engine_build_timeout_s=TRIAL_TIMEOUT_S)
    target = cache / ('nexo-n16-split-gpu-parity-result.json' if partition_gpu_parity else
                      'nexo-n16-stateful-cpu-parity-result.json' if partition_stateful_cpu_only else
                      'nexo-n16-partition-plan.json' if partition_plan_only else
                      'nexo-n16-partition-extract-result.json' if partition_extract_only else
                      'nexo-n16-partition-cpu-parity-result.json' if partition_parity_only else
                      'nexo-n16-partition-boundary-diagnosis-result.json' if partition_boundary_diagnose_only else
                      'nexo-n16-trial-result.json')
    timeline_target = (cache / 'nexo-n16-split-gpu-parity-memory.json' if partition_gpu_parity else
                       cache / 'nexo-n16-stateful-cpu-parity-memory.json' if partition_stateful_cpu_only else
                       cache / 'nexo-n16-partition-memory.json' if partition_plan_only else
                       cache / 'nexo-n16-partition-extract-memory.json' if partition_extract_only else
                       cache / 'nexo-n16-partition-cpu-parity-memory.json' if partition_parity_only else
                       cache / 'nexo-n16-partition-boundary-diagnosis-memory.json' if partition_boundary_diagnose_only else TIMELINE)
    started = time.monotonic()
    report['time_limit_s'] = timeout_s
    try:
      if sequence_trial:
        save_json(target, {**report, 'elapsed_s': 0, 'frame_progress': cpu_frame_progress('preflight')})
      child.start()
      until = started + timeout_s
      last_guard = 0.0
      previous_phase = None
      while child.is_alive():
        child.join(timeout=0.5)
        worker_status = read_report(stage / 'result.json')
        if sequence_trial:
          sampled = read_report(stage / 'stateful-cpu-rss.json').get(
            'maximum_controller_and_cpu_worker_rss_bytes_sampled', 0)
          report['maximum_controller_and_cpu_worker_rss_bytes_sampled'] = max(
            report.get('maximum_controller_and_cpu_worker_rss_bytes_sampled', 0), sampled)
        phase = worker_status.get('phase', 'preflight')
        if phase == 'onnx_patch_separate_process':
          phase = read_report(stage / 'prepatch-phase.json').get('phase', phase)
        timeline.record(system_sample(child.pid), phase)
        available_now = timeline.minimum_available_bytes
        if available_now is not None:
          report['minimum_memory_available_bytes'] = min(
            report['minimum_memory_available_bytes'], available_now)
        report['phase'] = phase
        report['elapsed_s'] = round(time.monotonic() - started, 1)
        if sequence_trial:
          report['frame_progress'] = cpu_frame_progress(phase)
        if previous_phase != phase or timeline.total_samples % 10 == 0:
          if sequence_trial:
            save_json(target, report)
          save_json(timeline_target, timeline.export())
        previous_phase = phase
        if (time.monotonic() - last_guard >= 3.0):
          if (guard() is not True or (ROOT / 'current').resolve() != release
              or (ROOT / 'updates/nexo-ncm.json').read_bytes() != config):
            raise RuntimeError('Offroad or installed release changed; stopping N16 trial')
          last_guard = time.monotonic()
        if time.monotonic() > until:
          raise TrialTimeLimit(f'N16 trial time limit reached ({timeout_s}s)')
        if shutil.disk_usage(cache).free < DISK_FLOOR:
          raise RuntimeError('N16 trial storage reserve below 1.5 GiB; stopping')
        if available_now is not None and available_now < MEMORY_FLOOR:
          raise RuntimeError('N16 trial memory reserve below 2 GiB; stopping')
      if child.exitcode != 0:
        raise RuntimeError('N16 worker terminated; see transient logs and kernel journal')
      if guard() is not True:
        raise RuntimeError('Offroad state lost at completion')
      report.update(json.loads((stage / 'result.json').read_text()))
      if sequence_trial:
        report['frame_progress'] = cpu_frame_progress(report['phase'])
      if partition_extract_only:
        if report.get('state') != 'models_extracted' or report.get('structural_verification_passed') is not True:
          raise RuntimeError('N16 extraction did not complete structural verification')
        if guard() is not True:
          raise RuntimeError('Offroad state lost before extracted-model publication')
        destination = partition_export_path(cache, MODEL_SHA, partition_cut)
        if destination.exists() or destination.is_symlink():
          raise RuntimeError('An N16 export already exists; refusing to overwrite or remove it')
        staged = stage / 'partition-export'
        if staged.is_symlink() or not staged.is_dir():
          raise RuntimeError('Partition staging directory disappeared')
        if shutil.disk_usage(cache).free < DISK_FLOOR:
          raise RuntimeError('Insufficient disk reserve for partition export')
        os.rename(staged, destination)
        report['export_dir'] = str(destination)
        report['left_model_path'] = str(destination / 'left.onnx')
        report['right_model_path'] = str(destination / 'right.onnx')
        report['production_engine_published'] = False
    except BaseException as error:
      report.update(state='timed_out' if sequence_trial and isinstance(error, TrialTimeLimit) else 'failed',
                    error=f'{type(error).__name__}: {error}'[:800])
      if (stage / 'result.json').is_file():
        report['worker_result'] = json.loads((stage / 'result.json').read_text())
    finally:
      if child.is_alive():
        try:
          if os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signal.SIGTERM)
          else:
            child.terminate()
        except ProcessLookupError:
          pass
        child.join(timeout=8)
        if child.is_alive():
          child.kill()
          child.join()
      # Research trial never publishes an engine and never touches production cache.
      timeline.record(system_sample(), report.get('phase', 'trial_finished'))
      save_json(timeline_target, timeline.export())
      report['elapsed_s'] = round(time.monotonic() - started, 1)
      report['memory_timeline_samples'] = timeline.total_samples
      report['minimum_memory_available_bytes'] = min(
        report['minimum_memory_available_bytes'],
        timeline.minimum_available_bytes or report['minimum_memory_available_bytes'])
      report['maximum_worker_rss_bytes_observed'] = max(timeline.maximum_worker_rss_bytes,
        report.get('maximum_controller_and_cpu_worker_rss_bytes_sampled', 0))
      if partition_gpu_parity:
        from openpilot.tools.jetson.nexo_n16_split_gpu import memory_summary
        report['gpu_telemetry'] = {role: read_report(stage / (role + '-telemetry.json'))
                                   for role in ('build_left', 'build_right', 'gpu_replay')}
        report['gpu_memory_summary'] = memory_summary(report['gpu_telemetry'])
        report['engine_build_attempted'] |= any(role in data.get('samples', {}) for role, data in report['gpu_telemetry'].items()
                                                if role in ('build_left', 'build_right'))
        if report['state'] not in ('gpu_parity_passed', 'gpu_parity_failed'):
          report.update(gpu_inference_verified=False, gpu_parity_verified=False)
      shutil.rmtree(stage)
      report.update(artifacts_deleted=True, production_engine_published=False)
      report['diagnosis'] = classify_trial(report)
      report['feasibility'] = assess_feasibility(report, timeline.export())
      report['hud_observer'] = hud_observer_contract(report)
      _store(target, report)
    return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--comma-ssh', required=True, help='Verified comma@192.168.100.127')
  parser.add_argument('--identity-file', help='Existing approved SSH private key, if needed')
  parser.add_argument('--samples', type=int, default=3)
  parser.add_argument('--timeout-s', type=int,
                      help=f'Whole-trial deadline in seconds (minimum 60). 140-frame CPU or split GPU: default {STATEFUL_CPU_TIMEOUT_S}, '
                           f'max {MAX_STATEFUL_CPU_TIMEOUT_S}. Other trials: default {TRIAL_TIMEOUT_S}, max {MAX_TRIAL_TIMEOUT_S}.')
  parser.add_argument('--workspace-mib', type=int, default=None,
                      help='Explicit TensorRT tactic workspace cap (256..1024 MiB); not a total GPU memory cap')
  parser.add_argument('--optimization-level', type=int, default=3, choices=(0, 1, 2, 3),
                      help='TensorRT builder tactic exploration level; 0 is minimum')
  parser.add_argument('--split-builder', action='store_true',
                      help='Patch ONNX in a separate CPU-only process before importing TensorRT')
  parser.add_argument('--tactic-dram-mib', type=int, choices=(512, 1024), default=None,
                      help='Opt-in TensorRT tactic DRAM budget; not total CUDA memory')
  parser.add_argument('--partition-plan-only', action='store_true',
                      help='CPU-only graph cut candidates; never build a TensorRT engine')
  parser.add_argument('--partition-extract-only', action='store_true',
                      help='Guarded CPU-only extraction of two pinned ONNX halves; no TRT, CAN or HUD')
  parser.add_argument('--partition-parity-only', action='store_true',
                      help='Guarded CPU-only N16 numerical comparison; requires existing verified halves')
  parser.add_argument('--partition-gpu-parity', action='store_true',
                      help='Offroad synthetic GPU trial using the accepted CPU cut 360; scratch engines only')
  parser.add_argument('--partition-stateful-cpu-only', action='store_true',
                      help='Offroad-only 140-frame raw N16 CPU comparison with explicit FP32 boundary transport; no GPU export')
  parser.add_argument('--partition-boundary-diagnose-only', action='store_true',
                      help='Guarded offline CPU-only error localization at eight cut tensors')
  parser.add_argument('--partition-cut', type=int, choices=(360, 361, 364), default=364,
                      help='N16 CPU partition cut; alternate cuts are limited to extraction/parity')
  args = parser.parse_args()
  from openpilot.tools.jetson.ssh_preflight import CommaGuard
  guard = CommaGuard(args.comma_ssh, args.identity_file, ROOT)
  print(json.dumps(trial(guard, samples=args.samples, timeout_s=args.timeout_s, workspace_mib=args.workspace_mib,
                         optimization_level=args.optimization_level,
                         split_builder=args.split_builder,
                          tactic_dram_mib=args.tactic_dram_mib,
                          partition_plan_only=args.partition_plan_only,
                          partition_extract_only=args.partition_extract_only,
                          partition_parity_only=args.partition_parity_only,
                          partition_boundary_diagnose_only=args.partition_boundary_diagnose_only,
                          partition_cut=args.partition_cut,
                          partition_stateful_cpu_only=args.partition_stateful_cpu_only,
                          partition_gpu_parity=args.partition_gpu_parity), ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
