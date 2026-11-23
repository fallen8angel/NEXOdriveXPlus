"""Real subprocess lifetimes and reference/replay equivalence; no vehicle or GPU."""
import json
import lzma
import os
import time

import pytest

from openpilot.tools.jetson.nexo_n16_cpu_precision import compare_sequence
from openpilot.tools.jetson.nexo_n16_cpu_workers import ReferenceReplay, isolated_phase, write_reference
from openpilot.tools.jetson.nexo_n16_diagnostics import save_json, proc_memory_bytes
from openpilot.tools.jetson.tests.test_nexo_n16_cpu_precision import sequence_sessions, SPEC


def _fixture_spec(session):
  return {**SPEC, 'sha256': 'f' * 64,
          'input_shapes': {v.name: v.shape for v in session.get_inputs()}}


def test_real_140_frame_record_and_replay_preserves_all_states_and_inputs(tmp_path):
  reference, left, right = sequence_sessions()
  spec = _fixture_spec(reference)
  path = tmp_path / 'reference.xz'
  report = write_reference(reference, spec, path, lambda n: None)
  assert report['frames'] == 140 and report['journal_bytes'] > 0
  replay = ReferenceReplay(path, spec)
  try:
    result = compare_sequence(replay, left, right, spec)
    replay.finish()
    assert result['stateful_140_frames_verified'] and result['frames_completed'] == 140
    assert result['distinct_image_frames'] == 140
    assert max(result['max_abs_error_by_output'].values()) == 0
    assert not result['gpu_inference_verified']
  finally:
    replay.close()


def test_reference_replay_rejects_changed_inputs_or_state(tmp_path):
  from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import synthetic_feeds
  reference, _, _ = sequence_sessions()
  spec = _fixture_spec(reference)
  path = tmp_path / 'reference.xz'
  write_reference(reference, spec, path, lambda n: None, frames=2)
  replay = ReferenceReplay(path, spec, frames=2)
  feeds = synthetic_feeds(replay.get_inputs(), 0)
  feeds['state_feat_q'].fill(1)
  try:
    with pytest.raises(ValueError, match='sequence, recurrent state or frame order'):
      replay.run(None, feeds)
  finally:
    replay.close()


@pytest.mark.parametrize('kind', ['short', 'source', 'truncated'])
def test_short_changed_or_damaged_journal_never_passes(tmp_path, kind):
  reference, left, right = sequence_sessions()
  spec = _fixture_spec(reference)
  path = tmp_path / 'reference.xz'
  write_reference(reference, spec, path, lambda n: None, frames=3)
  if kind == 'short':
    with pytest.raises(ValueError, match='frames'):
      ReferenceReplay(path, spec)
    return
  if kind == 'source':
    with pytest.raises(ValueError, match='source'):
      ReferenceReplay(path, {**spec, 'sha256': '0' * 64}, frames=3)
    return
  path.write_bytes(path.read_bytes()[:-16])
  with pytest.raises((ValueError, EOFError, lzma.LZMAError)):
    replay = ReferenceReplay(path, spec, frames=3)
    try:
      compare_sequence(replay, left, right, spec, frames=3)
      replay.finish()
    finally:
      replay.close()


def test_reference_journal_is_bounded_and_keeps_storage_reserve(tmp_path, monkeypatch):
  from types import SimpleNamespace
  import openpilot.tools.jetson.nexo_n16_cpu_workers as workers
  reference, _, _ = sequence_sessions()
  monkeypatch.setattr(workers.shutil, 'disk_usage', lambda p: SimpleNamespace(free=1 << 30))
  with pytest.raises(RuntimeError, match='storage reserve'):
    write_reference(reference, _fixture_spec(reference), tmp_path / 'reference.xz', lambda n: None, frames=2)


def _small_process(role, stage):
  from openpilot.tools.jetson.nexo_n16_cpu_workers import _single_rss
  # Touch pages so actual process RSS, rather than only virtual size, grows.
  allocation = bytearray(32 << 20)
  allocation[::4096] = b'\1' * (len(allocation) // 4096)
  rss = proc_memory_bytes(os.getpid())['worker_rss_bytes']
  save_json(stage / 'phase.json', {'phase': role + '_active'})
  save_json(stage / (role + '-result.json'), {'pid': os.getpid(), 'rss': rss,
            'controller_rss': _single_rss(os.getppid()), 'time': time.monotonic()})
  time.sleep(0.6)


def test_reference_process_has_exited_before_split_can_start(tmp_path):
  first = isolated_phase(_small_process, ('reference', tmp_path), 'reference', tmp_path, lambda p: None)
  with pytest.raises(ProcessLookupError):
    os.kill(first['pid'], 0)
  second = isolated_phase(_small_process, ('split', tmp_path), 'split', tmp_path, lambda p: None)
  assert first['pid'] != second['pid'] and first['time'] < second['time']
  assert first['rss'] > 32 << 20 and second['rss'] > 32 << 20
  for result in (first, second):
    assert result['controller_and_cpu_worker_rss_bytes_sampled'] + (8 << 20) >= result['rss'] + result['controller_rss']
  with pytest.raises(ProcessLookupError):
    os.kill(second['pid'], 0)


def test_guard_exception_terminates_and_joins_cpu_worker(tmp_path):
  def abort(phase):
    raise RuntimeError('host memory guard')
  with pytest.raises(RuntimeError, match='host memory'):
    isolated_phase(_small_process, ('reference', tmp_path), 'reference', tmp_path, abort)
  result = json.loads((tmp_path / 'reference-result.json').read_text())
  with pytest.raises(ProcessLookupError):
    os.kill(result['pid'], 0)


def test_existing_parent_stops_low_memory_and_preserves_models_and_release(tmp_path, monkeypatch):
  from types import SimpleNamespace
  import openpilot.tools.jetson.nexo_n16_trt_trial as trial
  root = tmp_path
  (root / 'cache').mkdir()
  release = root / 'releases/old'
  release.mkdir(parents=True)
  (root / 'current').symlink_to(release)
  (root / 'updates').mkdir()
  config = root / 'updates/nexo-ncm.json'
  config.write_bytes(b'existing')
  model = root / 'cache/model.onnx'
  model.write_bytes(b'original model')
  engine = root / 'cache/normal.plan'
  engine.write_bytes(b'normal engine')
  class Process:
    pid = 987654
    exitcode = None
    alive = False
    def start(self): self.alive = True
    def is_alive(self): return self.alive
    def join(self, timeout=None): pass
    def terminate(self): self.alive = False
    def kill(self): self.alive = False
  child = Process()
  monkeypatch.setattr(trial, 'ROOT', root)
  monkeypatch.setattr(trial, 'readiness', lambda *a, **k: (release, b'existing', 10 << 30, 6 << 30))
  monkeypatch.setattr(trial.multiprocessing, 'get_context', lambda *a: SimpleNamespace(Process=lambda **kw: child))
  monkeypatch.setattr(trial.shutil, 'disk_usage', lambda p: SimpleNamespace(free=10 << 30))
  monkeypatch.setattr(trial.os, 'getpgid', lambda pid: 0)
  monkeypatch.setattr(trial, 'system_sample', lambda *a: {
    'MemAvailable_bytes': trial.MEMORY_FLOOR - 1, 'worker_rss_bytes': 100})
  result = trial.trial(lambda: True, partition_stateful_cpu_only=True, partition_cut=360)
  assert result['state'] == 'failed' and result['diagnosis']['diagnosis_code'] == 'host_memory_guard'
  assert trial.MEMORY_FLOOR == 2 << 30 and not child.is_alive()
  assert model.read_bytes() == b'original model' and engine.read_bytes() == b'normal engine'
  assert (root / 'current').resolve() == release
