"""Bounded CPU reference journal and non-overlapping N16 validation processes.

No model/engine writes, GPU imports or operational service changes. The outer
trial still owns Offroad, exclusive lock, RAM/disk floors and time limits.
"""
from __future__ import annotations

import hashlib
import io
import json
import lzma
import multiprocessing
import os
from pathlib import Path
import resource
import shutil
import struct
import tempfile
import time
from types import SimpleNamespace

from openpilot.tools.jetson.nexo_n16_cpu_precision import FRAMES

MAX_RECORD = 64 << 20
MAX_JOURNAL = 256 << 20
MAGIC = b'NEXO-N16-CPU-1\n'


def input_digest(feeds):
  digest = hashlib.sha256()
  for name, value in sorted(feeds.items()):
    digest.update(name.encode() + b'\0' + value.dtype.str.encode() + b'\0')
    digest.update(json.dumps(list(value.shape)).encode())
    digest.update(value.tobytes(order='C'))
  return digest.digest()


def _descriptors(items):
  return [{'name': v.name, 'shape': list(v.shape), 'type': v.type} for v in items]


def write_reference(session, spec, destination, on_frame, *, frames=FRAMES):
  """One sequential recurrent reference, one compressed record per frame."""
  import numpy as np
  from openpilot.tools.jetson.nexo_n16_cpu_precision import _feeds, sequence_feeds
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import synthetic_feeds
  from openpilot.tools.jetson.nexo_n16_trt_trial import DISK_FLOOR
  seeds = synthetic_feeds(session.get_inputs(), 0)
  states = {k: v for k, v in seeds.items() if k.startswith('state_')}
  header = json.dumps({'frames': frames, 'source_sha256': spec['sha256'],
                       'inputs': _descriptors(session.get_inputs()),
                       'outputs': _descriptors(session.get_outputs())}).encode()
  if len(header) > 16384 or not 1 <= frames <= FRAMES:
    raise ValueError('Unbounded reference header or frame count')
  compressor = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64,
                                   filters=[{'id': lzma.FILTER_LZMA2, 'preset': 0, 'dict_size': 16 << 20}])
  with destination.open('xb') as raw:
    def emit(data):
      if raw.tell() + len(data) > MAX_JOURNAL:
        raise ValueError('Reference journal exceeds storage budget')
      if shutil.disk_usage(destination.parent).free < DISK_FLOOR + len(data) + 65536:
        raise RuntimeError('CPU reference journal storage reserve below safe floor')
      raw.write(data)
    emit(compressor.compress(MAGIC + struct.pack('<I', len(header)) + header))
    for frame, external in sequence_feeds(seeds, frames):
      on_frame(frame)
      feeds = _feeds(session, {**external, **states})
      start = time.monotonic()
      outputs = dict(zip([v.name for v in session.get_outputs()], session.run(None, feeds), strict=True))
      elapsed_ms = (time.monotonic() - start) * 1000
      if set(outputs) != set(spec['output_shapes']) or any(not np.isfinite(v).all() for v in outputs.values()):
        raise ValueError('Invalid reference output contract or nonfinite result')
      states = {name: outputs['next_' + name] for name in states}
      packet = io.BytesIO()
      np.savez(packet, **outputs, __frame=np.array([frame], np.int64),
               __input_sha=np.frombuffer(input_digest(feeds), np.uint8), __reference_ms=np.array([elapsed_ms]))
      value = packet.getvalue()
      if len(value) > MAX_RECORD:
        raise ValueError('Reference frame exceeds memory budget')
      emit(compressor.compress(struct.pack('<I', len(value)) + value))
    emit(compressor.flush())
  return {'frames': frames, 'journal_bytes': destination.stat().st_size}


class ReferenceReplay:
  """CPU-session interface backed by bounded, CRC-checked reference records."""
  def __init__(self, path, spec, *, frames=FRAMES):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_JOURNAL:
      raise ValueError('Unsafe CPU reference journal')
    self.file = lzma.open(path, 'rb')
    self.frame = 0
    self.frames = frames
    self.spec = spec
    try:
      if self._read(len(MAGIC)) != MAGIC:
        raise ValueError('Invalid reference journal signature')
      size = struct.unpack('<I', self._read(4))[0]
      if not 0 < size <= 16384:
        raise ValueError('Unbounded CPU reference metadata')
      header = json.loads(self._read(size))
      self.inputs = [SimpleNamespace(**v) for v in header['inputs']]
      self.outputs = [SimpleNamespace(**v) for v in header['outputs']]
      if (header['source_sha256'] != spec['sha256'] or header['frames'] != frames
          or len(self.inputs) != len(spec['input_shapes']) or len(self.outputs) != len(spec['output_shapes'])
          or {v.name: v.shape for v in self.inputs} != spec['input_shapes']
          or {v.name: v.shape for v in self.outputs} != spec['output_shapes']):
        raise ValueError('Reference source, frames or IO contract changed')
    except BaseException:
      self.close()
      raise

  def _read(self, count):
    result = self.file.read(count)
    if len(result) != count:
      raise ValueError('Truncated CPU reference journal')
    return result

  def get_inputs(self):
    return self.inputs

  def get_outputs(self):
    return self.outputs

  def run(self, names, feeds):
    import numpy as np
    if self.frame >= self.frames:
      raise ValueError('Unexpected reference frame')
    length = struct.unpack('<I', self._read(4))[0]
    if not 0 < length <= MAX_RECORD:
      raise ValueError('Unbounded reference record')
    with np.load(io.BytesIO(self._read(length)), allow_pickle=False) as archive:
      expected = set(self.spec['output_shapes']) | {'__frame', '__input_sha', '__reference_ms'}
      if len(archive.files) != len(expected) or set(archive.files) != expected:
        raise ValueError('Missing or duplicate CPU reference outputs')
      if (archive['__frame'].tolist() != [self.frame]
          or archive['__input_sha'].tobytes() != input_digest(feeds)):
        raise ValueError('CPU input sequence, recurrent state or frame order changed')
      latency = archive['__reference_ms']
      if latency.shape != (1,) or not np.isfinite(latency).all() or latency[0] < 0:
        raise ValueError('Invalid recorded CPU latency')
      self.last_inference_ms = float(latency[0])
      values = {name: archive[name] for name in self.spec['output_shapes']}
    self.frame += 1
    requested = names if names is not None else [v.name for v in self.outputs]
    return [values[name] for name in requested]

  def finish(self):
    if self.frame != self.frames or self.file.read(1):
      raise ValueError('Incomplete or extra CPU reference frames')

  def close(self):
    self.file.close()


def _cpu_child(role, source, release, cut, stage, gpu_trial=False, trace_targets=()):
  from openpilot.tools.jetson.nexo_n16_cpu_precision import build_sessions, compare_sequence
  from openpilot.tools.jetson.nexo_n16_diagnostics import proc_memory_bytes, save_json
  marks = []
  replay = None
  def phase(name):
    current = {**proc_memory_bytes(os.getpid()),
               'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    if 'frame_' not in name or int(name.rsplit('_', 1)[1]) % 10 == 0:
      marks.append({'phase': name, **current})
    save_json(stage / 'phase.json', {'phase': name, 'role': role, 'pid': os.getpid(),
                                    'current': current, 'memory_marks': marks})
  try:
    if gpu_trial:
      # Leave scheduling headroom for the parent monitoring SSH and shared RAM.
      nice = os.nice(5)
      if role == 'reference':
        from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import load_cpu_ort
        ort = load_cpu_ort()
        save_json(stage / 'cpu-runtime.json', {'package': str(Path(ort.__file__).resolve().parent),
                  'version': ort.__version__, 'provider': 'CPUExecutionProvider', 'worker_nice': nice})
    phase(role + '_before_load')
    if role in ('trace_original', 'trace_patched'):
      from openpilot.tools.jetson.nexo_n16_gpu_trace import trace_cpu
      result = trace_cpu(stage, trace_targets, patched=role == 'trace_patched')
      if role == 'trace_original' and tuple(trace_targets) == ('conv-isolated',):
        from openpilot.tools.jetson.nexo_n16_conv_isolated import prepare_isolated_conv
        phase('isolated_conv_cpu_oracle')
        result['isolated_conv_cpu'] = prepare_isolated_conv(stage)
    elif role == 'probe':
      from openpilot.tools.jetson.nexo_n16_registry import load_registry
      from openpilot.tools.jetson.nexo_n16_cpu_plan import probe_first_frame
      phase('stateful_cpu_probe_initial_boundary')
      result = {'plan': probe_first_frame(stage, load_registry()[0], reference_frames=1 if trace_targets else FRAMES)}
    else:
      sessions, receipts, spec = build_sessions(source, release, cut,
                                               ('reference',) if role == 'reference' else ('left', 'right'), phase, stage=stage)
    if role == 'reference':
      result = write_reference(sessions[0], spec, stage / 'reference.xz',
                               lambda n: phase('stateful_cpu_reference_frame_' + str(n)), frames=1 if trace_targets else FRAMES)
    elif role == 'split':
      from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import load_arrays
      boundary = load_arrays(stage / 'first-boundary.npz') if cut in (360, 361) else None
      if boundary is not None and set(boundary) != {v['tensor'] for v in receipts[0]['boundary']}:
        raise ValueError('Missing first-frame boundary evidence')
      replay = ReferenceReplay(stage / 'reference.xz', spec)
      result = compare_sequence(replay, *sessions, spec,
                                 on_frame=lambda n: phase('stateful_cpu_frame_' + str(n)), first_boundary=boundary)
      replay.finish()
      if cut in (360, 361):
        result.update(phase='n16_stateful_resolved_cpu_handoff', initializer_integrity_verified=True,
                      boundary_transport=receipts[0])
      else:
        result.update(
          initializer_integrity_verified=all(r['source_initializer_manifest_sha256'] ==
                                             receipts[0]['source_initializer_manifest_sha256'] for r in receipts),
          local_function_definitions_preserved=all(r['local_function_definitions_preserved'] for r in receipts),
          boundary_transport=receipts[0]['transport'], right_boundary_transport=receipts[1]['transport'])
    result.update(memory_marks=marks, pid=os.getpid(),
                  peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                  ort_version=__import__('onnxruntime').__version__)
    save_json(stage / (role + '-result.json'), result)
  except BaseException as error:
    save_json(stage / (role + '-failure.json'), {'error': f'{type(error).__name__}: {error}'[:500], 'memory_marks': marks})
    raise
  finally:
    if replay is not None:
      replay.close()


def _single_rss(pid):
  try:
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
      if line.startswith('VmRSS:'):
        return int(line.split()[1]) * 1024
  except (OSError, ValueError):
    pass
  return 0


def isolated_phase(target, args, role, stage, on_phase, *, sample_path=None, timeout_s=None):
  """Join a worker completely before its caller can launch the next phase."""
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report, save_json
  child = multiprocessing.get_context('spawn').Process(target=target, args=args)
  peak_tree, previous, reported_peak = 0, None, None
  try:
    (stage / 'phase.json').unlink(missing_ok=True)  # This run's private phase marker only.
    child.start()
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    while child.is_alive():
      child.join(0.25)
      if deadline is not None and time.monotonic() >= deadline and child.is_alive():
        raise TimeoutError(f'Offline {role} worker exceeded {timeout_s}s')
      # Some managed kernels omit /proc/PID/task/PID/children. Track the known
      # session owner explicitly, and avoid double-counting it on normal Linux.
      peak_tree = max(peak_tree, _single_rss(os.getpid()) + _single_rss(child.pid))
      if sample_path is not None and peak_tree != reported_peak:
        save_json(sample_path, {'maximum_controller_and_cpu_worker_rss_bytes_sampled': peak_tree})
        reported_peak = peak_tree
      phase = read_report(stage / 'phase.json').get('phase')
      if phase and phase != previous:
        on_phase(phase)
        previous = phase
    if child.exitcode != 0:
      failure = read_report(stage / (role + '-failure.json'))
      raise RuntimeError(f'Isolated {role} worker failed: ' + str(failure.get('error') or child.exitcode))
    result = read_report(stage / (role + '-result.json'))
    if not result:
      raise RuntimeError(f'Isolated {role} worker result missing')
    result['controller_and_cpu_worker_rss_bytes_sampled'] = peak_tree
    return result
  finally:
    if child.is_alive():
      child.terminate()
      child.join(8)
      if child.is_alive():
        child.kill()
        child.join()
    child.close()


def run_serial_cpu(source, release, cut, on_phase, *, stage=None):
  from openpilot.tools.jetson.nexo_n16_diagnostics import read_report, save_json
  from openpilot.tools.jetson.nexo_n16_registry import load_registry
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import digest
  from openpilot.tools.jetson.nexo_n16_partition_extract import ALLOWED_N16_CUTS
  spec, _ = load_registry()
  if (type(cut) is not int or cut not in ALLOWED_N16_CUTS or source.is_symlink()
      or not source.is_file() or source.stat().st_size != spec['nbytes'] or digest(source) != spec['sha256']):
    raise ValueError('Pinned N16 source identity or CPU cut changed')
  with tempfile.TemporaryDirectory(prefix='n16-cpu-sequence-', dir=stage or source.parent) as temporary:
    scratch = Path(temporary)
    progress = {}
    def forward(name):
      sample = read_report(scratch / 'phase.json')
      if sample.get('role') in ('reference', 'probe', 'split'):
        progress[sample['role']] = sample
      if stage is not None:
        save_json(stage / 'stateful-cpu-memory.json', progress)
      on_phase(name)
    sample_path = (stage or scratch) / 'stateful-cpu-rss.json'
    reference = isolated_phase(_cpu_child, ('reference', source, release, cut, scratch),
                               'reference', scratch, forward, sample_path=sample_path)
    if reference.get('frames') != FRAMES:
      raise ValueError('Incomplete reference sequence')
    # Process exit and join have returned ALL ORT/native allocator memory.
    on_phase('stateful_cpu_reference_process_exited')
    probe = {}
    if cut in (360, 361):
      probe = isolated_phase(_cpu_child, ('probe', source, release, cut, scratch),
                             'probe', scratch, forward, sample_path=sample_path)
    result = isolated_phase(_cpu_child, ('split', source, release, cut, scratch),
                            'split', scratch, forward, sample_path=sample_path)
    if any(p['ort_version'] != reference['ort_version'] for p in (result, probe) if p):
      raise ValueError('CPU runtime changed between reference and split phases')
    result.update(model_sha256=spec['sha256'], cut_node_index=cut,
                  execution_layout='reference_process_exit_then_split_process',
                  reference_journal_bytes=reference['journal_bytes'],
                  memory_by_stage={'reference': reference, 'split': {
                    'memory_marks': result.pop('memory_marks'), 'pid': result['pid'],
                    'peak_process_rss_bytes': result['peak_process_rss_bytes']}},
                  peak_process_rss_bytes=max(reference['peak_process_rss_bytes'], result['peak_process_rss_bytes']),
                  maximum_controller_and_cpu_worker_rss_bytes_sampled=max(
                    reference['controller_and_cpu_worker_rss_bytes_sampled'],
                    result['controller_and_cpu_worker_rss_bytes_sampled']))
    if probe:
      result['execution_layout'] = 'serial_reference_probe_split'
      result['memory_by_stage']['probe'] = {k: probe[k] for k in ('memory_marks', 'pid', 'peak_process_rss_bytes')}
      result['peak_process_rss_bytes'] = max(result['peak_process_rss_bytes'], probe['peak_process_rss_bytes'])
      key = 'maximum_controller_and_cpu_worker_rss_bytes_sampled'
      result[key] = max(result[key], probe['controller_and_cpu_worker_rss_bytes_sampled'])
    return result
