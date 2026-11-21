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
DISK_FLOOR = 1536 * 1024 ** 2
MEMORY_MIN = 5 * 1024 ** 3
MEMORY_FLOOR = 2 * 1024 ** 3
SAMPLES_MAX = 10


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
            partition_plan_only: bool = False):
  os.setsid()
  if tactic_dram_mib is not None:
    os.environ.setdefault('CUDA_MODULE_LOADING', 'LAZY')
  status = {'state': 'running', 'phase': 'imports', 'gpu_inference_verified': False,
            'vehicle_control_enabled': False, 'hud_prediction_enabled': False,
            'production_engine_published': False}
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


def readiness(guard, root: Path = ROOT):
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
  if free < DISK_MIN or memory < MEMORY_MIN:
    raise RuntimeError(f'Insufficient build reserves: free_disk={free}, MemAvailable={memory}. '
                       'Do not delete protected caches or rollback files.')
  return release, configuration, free, memory


def trial(guard, samples: int = 3, timeout_s: int = 900,
          workspace_mib: int | None = None, optimization_level: int = 3,
          split_builder: bool = False, tactic_dram_mib: int | None = None,
          partition_plan_only: bool = False):
  if not 2 <= samples <= SAMPLES_MAX or not 60 <= timeout_s <= 1200:
    raise ValueError('Invalid samples or time budget')
  workspace = validate_build_profile(workspace_mib, optimization_level)
  from openpilot.tools.jetson.nexo_n16_trt_memory import validate_tactic_dram_mib
  validate_tactic_dram_mib(tactic_dram_mib)
  if type(split_builder) is not bool or type(partition_plan_only) is not bool:
    raise ValueError('split_builder and partition_plan_only must be booleans')
  if partition_plan_only and (split_builder or workspace_mib is not None
                              or optimization_level != 3 or tactic_dram_mib is not None):
    raise ValueError('CPU-only partition planning must not have TensorRT build options')
  release, config, free, memory = readiness(guard)
  cache = ROOT / 'cache'
  lockpath = cache / 'nexo-engine-preparation.lock'
  if lockpath.is_symlink():
    raise RuntimeError('Unsafe engine lock')
  with lockpath.open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stage = Path(tempfile.mkdtemp(prefix='nexo-n16-trial-', dir=cache))
    child = multiprocessing.get_context('spawn').Process(
      target=_worker, args=(release, stage, samples, workspace, optimization_level, split_builder,
                            tactic_dram_mib, partition_plan_only))
    timeline = Timeline()
    report = {'state': 'running', 'phase': 'preflight', 'model_sha256': MODEL_SHA,
              'available_disk': free, 'available_memory': memory,
              'workspace_mib': workspace_mib if workspace_mib is not None else 'vendor_default',
              'optimization_level': optimization_level, 'split_builder': split_builder,
              'tactic_dram_mib': tactic_dram_mib, 'partition_plan_only': partition_plan_only,
              'minimum_memory_available_bytes': memory,
              'vehicle_control_enabled': False, 'production_engine_published': False}
    target = cache / ('nexo-n16-partition-plan.json' if partition_plan_only else 'nexo-n16-trial-result.json')
    timeline_target = cache / 'nexo-n16-partition-memory.json' if partition_plan_only else TIMELINE
    try:
      child.start()
      until = time.monotonic() + timeout_s
      last_guard = 0.0
      previous_phase = None
      while child.is_alive():
        child.join(timeout=0.5)
        worker_status = read_report(stage / 'result.json')
        phase = worker_status.get('phase', 'preflight')
        if phase == 'onnx_patch_separate_process':
          phase = read_report(stage / 'prepatch-phase.json').get('phase', phase)
        timeline.record(system_sample(child.pid), phase)
        available_now = timeline.minimum_available_bytes
        if available_now is not None:
          report['minimum_memory_available_bytes'] = min(
            report['minimum_memory_available_bytes'], available_now)
        if previous_phase != phase or timeline.total_samples % 10 == 0:
          save_json(timeline_target, timeline.export())
        previous_phase = phase
        report['phase'] = phase
        if (time.monotonic() - last_guard >= 3.0):
          if (guard() is not True or (ROOT / 'current').resolve() != release
              or (ROOT / 'updates/nexo-ncm.json').read_bytes() != config):
            raise RuntimeError('Offroad or installed release changed; stopping N16 trial')
          last_guard = time.monotonic()
        if time.monotonic() > until:
          raise TimeoutError('N16 trial time limit reached')
        if shutil.disk_usage(cache).free < DISK_FLOOR:
          raise RuntimeError('N16 trial storage reserve below 1.5 GiB; stopping')
        if available_now is not None and available_now < MEMORY_FLOOR:
          raise RuntimeError('N16 trial memory reserve below 2 GiB; stopping')
      if child.exitcode != 0:
        raise RuntimeError('N16 worker terminated; see transient logs and kernel journal')
      if guard() is not True:
        raise RuntimeError('Offroad state lost at completion')
      report.update(json.loads((stage / 'result.json').read_text()))
    except BaseException as error:
      report.update(state='failed', error=f'{type(error).__name__}: {error}'[:800])
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
      report['memory_timeline_samples'] = timeline.total_samples
      report['minimum_memory_available_bytes'] = min(
        report['minimum_memory_available_bytes'],
        timeline.minimum_available_bytes or report['minimum_memory_available_bytes'])
      report['maximum_worker_rss_bytes_observed'] = timeline.maximum_worker_rss_bytes
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
  args = parser.parse_args()
  from openpilot.tools.jetson.ssh_preflight import CommaGuard
  guard = CommaGuard(args.comma_ssh, args.identity_file, ROOT)
  print(json.dumps(trial(guard, samples=args.samples, workspace_mib=args.workspace_mib,
                         optimization_level=args.optimization_level,
                         split_builder=args.split_builder,
                          tactic_dram_mib=args.tactic_dram_mib,
                          partition_plan_only=args.partition_plan_only), ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
