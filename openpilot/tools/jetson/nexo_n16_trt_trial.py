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
import shutil
import signal
import sys
import tempfile
import time

ROOT = Path('/opt/carrot-jetlink')
N16_SOURCE = ROOT / 'cache/nexo-n16-download/carrot-n16.onnx'
MODEL_SHA = '01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a'
MODEL_BYTES = 814591107
DISK_MIN = 4 * 1024 ** 3
DISK_FLOOR = 1536 * 1024 ** 2
MEMORY_MIN = 5 * 1024 ** 3
SAMPLES_MAX = 10


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


def _vendor(release: Path):
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
  onnx_patch.IMG_INPUTS = tuple(set(onnx_patch.IMG_INPUTS) | {'new_img', 'state_img_q'})
  onnx_patch.patch_uint8_inputs = patch_stateful_images
  return build


def _store(path: Path, value: dict):
  temporary = path.with_suffix('.tmp')
  temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
  os.replace(temporary, path)


def _worker(release: Path, stage: Path, samples: int):
  os.setsid()
  status = {'state': 'running', 'phase': 'imports', 'gpu_inference_verified': False,
            'vehicle_control_enabled': False, 'hud_prediction_enabled': False,
            'production_engine_published': False}
  engine = None
  try:
    import numpy as np
    import tensorrt as trt
    from openpilot.tools.jetson.nexo_n16_registry import load_registry
  except BaseException as error:
    status.update(state='failed', error=f'{type(error).__name__}: {error}'[:600])
    _store(stage / 'result.json', status)
    return
  try:
    spec, _ = load_registry()
    if spec['sha256'] != MODEL_SHA or spec['nbytes'] != MODEL_BYTES:
      raise ValueError('Pinned N16 contract mismatch')
    if trt.__version__ != '10.3.0':
      raise ValueError('Unverified TensorRT version')
    if digest(N16_SOURCE) != MODEL_SHA:
      raise ValueError('N16 source SHA mismatch before build')
    build = _vendor(release)
    plan = stage / 'n16-trial.plan'
    status['phase'] = 'tensorrt_build'
    _store(stage / 'result.json', status)
    build.build_engine(N16_SOURCE, plan, fp16=True, optimization_level=3,
                       meta_extra={'experimental': True, 'vehicle_control_enabled': False})
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
    status.update(state='passed', phase='synthetic_inference',
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


def trial(guard, samples: int = 3, timeout_s: int = 900):
  if not 2 <= samples <= SAMPLES_MAX or not 60 <= timeout_s <= 1200:
    raise ValueError('Invalid samples or time budget')
  release, config, free, memory = readiness(guard)
  cache = ROOT / 'cache'
  lockpath = cache / 'nexo-engine-preparation.lock'
  if lockpath.is_symlink():
    raise RuntimeError('Unsafe engine lock')
  with lockpath.open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stage = Path(tempfile.mkdtemp(prefix='nexo-n16-trial-', dir=cache))
    child = multiprocessing.get_context('spawn').Process(target=_worker, args=(release, stage, samples))
    report = {'state': 'running', 'phase': 'preflight', 'model_sha256': MODEL_SHA,
              'available_disk': free, 'available_memory': memory,
              'vehicle_control_enabled': False, 'production_engine_published': False}
    target = cache / 'nexo-n16-trial-result.json'
    try:
      child.start()
      until = time.monotonic() + timeout_s
      while child.is_alive():
        child.join(timeout=3)
        if (guard() is not True or (ROOT / 'current').resolve() != release
            or (ROOT / 'updates/nexo-ncm.json').read_bytes() != config):
          raise RuntimeError('Offroad or installed release changed; stopping N16 trial')
        if time.monotonic() > until:
          raise TimeoutError('N16 trial time limit reached')
        if shutil.disk_usage(cache).free < DISK_FLOOR:
          raise RuntimeError('N16 trial storage reserve below 1.5 GiB; stopping')
        if mem_available() < 1024 ** 3:
          raise RuntimeError('N16 trial memory reserve below 1 GiB; stopping')
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
      shutil.rmtree(stage)
      report.update(artifacts_deleted=True, production_engine_published=False)
      _store(target, report)
    return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--comma-ssh', required=True, help='Verified comma@192.168.100.127')
  parser.add_argument('--identity-file', help='Existing approved SSH private key, if needed')
  parser.add_argument('--samples', type=int, default=3)
  args = parser.parse_args()
  from openpilot.tools.jetson.ssh_preflight import CommaGuard
  guard = CommaGuard(args.comma_ssh, args.identity_file, ROOT)
  print(json.dumps(trial(guard, samples=args.samples), ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
