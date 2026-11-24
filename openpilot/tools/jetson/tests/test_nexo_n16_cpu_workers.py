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


@pytest.mark.parametrize('stop', ['low_memory', 'time_limit', 'gpu_ssh_timeout'])
def test_guarded_parent_records_progress_and_preserves_models_and_release(tmp_path, monkeypatch, stop):
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
  clock = [100.0]
  snapshots = []
  gpu = stop == 'gpu_ssh_timeout'
  result_file = root / 'cache' / ('nexo-n16-split-gpu-parity-result.json' if gpu else 'nexo-n16-stateful-cpu-parity-result.json')
  class Process:
    pid = 987654
    exitcode = None
    alive = False
    def start(self):
      self.alive = True
      self.stage = next((root / 'cache').glob('nexo-n16-trial-*'))
    def is_alive(self): return self.alive
    def join(self, timeout=None):
      if self.alive:
        snapshots.append(json.loads(result_file.read_text()))
        clock[0] += 950
        phase = 'stateful_cpu_reference_frame_139' if len(snapshots) == 1 else 'stateful_cpu_frame_6'
        if gpu:
          phase = 'stateful_cpu_reference_frame_27'
          save_json(self.stage / 'cpu-runtime.json', {'package': '/opt/carrot-jetlink/cache/nexo-ort-validation-deps/onnxruntime'})
        save_json(self.stage / 'result.json', {'state': 'running', 'phase': phase})
    def terminate(self): self.alive = False
    def kill(self): self.alive = False
  child = Process()
  monkeypatch.setattr(trial, 'ROOT', root)
  monkeypatch.setattr(trial, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
  monkeypatch.setattr(trial, 'readiness', lambda *a, **k: (release, b'existing', 10 << 30, 6 << 30))
  monkeypatch.setattr(trial.multiprocessing, 'get_context', lambda *a: SimpleNamespace(Process=lambda **kw: child))
  monkeypatch.setattr(trial.shutil, 'disk_usage', lambda p: SimpleNamespace(free=10 << 30))
  monkeypatch.setattr(trial.os, 'getpgid', lambda pid: 0)
  monkeypatch.setattr(trial, 'system_sample', lambda *a: {
    'MemAvailable_bytes': trial.MEMORY_FLOOR - 1 if stop == 'low_memory' else 5 << 30, 'worker_rss_bytes': 100})
  class Guard:
    def __call__(self):
      return True
    def live_state(self):
      from openpilot.tools.jetson.ssh_preflight import SSHFailure
      self.last = {'ok': False, 'code': 'SSH_TIMEOUT', 'stage': 'ssh_command', 'remote_step': 'remote_IsOffroad',
                   'recommendation': 'check device', 'elapsed_ms': 7000}
      raise SSHFailure(self.last)
  messages = []
  result = trial.trial(Guard(), partition_stateful_cpu_only=not gpu, partition_gpu_parity=gpu, partition_cut=360,
                       on_progress=messages.append)
  assert messages[0].startswith('N16 preflight')
  if gpu:
    assert any('28/140' in m and 'min_free=' in m for m in messages)
  if gpu:
    assert result['diagnosis']['diagnosis_code'] == 'offline_gpu_ssh_guard_failed'
    assert result['frame_progress']['current_frame'] == 28 and result['phase'] == 'stateful_cpu_reference_frame_27'
    assert result['ssh_guard']['remote_step'] == 'remote_IsOffroad' and result['ssh_guard']['elapsed_ms'] == 7000
    assert result['cpu_runtime']['package'].endswith('/nexo-ort-validation-deps/onnxruntime')
    assert not result['engine_build_attempted'] and not result['gpu_parity_verified']
  elif stop == 'low_memory':
    assert result['state'] == 'failed' and result['diagnosis']['diagnosis_code'] == 'host_memory_guard'
  else:
    from openpilot.tools.jetson.manage import n16_partition_parity_status
    # Reference is still running past the former 900s deadline. Frame indices in the phase
    # marker are zero based and mark frame entry, not successful comparison.
    assert snapshots[1]['state'] == 'running' and snapshots[1]['elapsed_s'] == 950
    assert snapshots[1]['frame_progress'] == {'stage': 'reference', 'current_frame': 140, 'frames_per_stage': 140}
    assert result['state'] == 'timed_out'
    status = n16_partition_parity_status(root)['stateful_cpu_validation']
    assert status['diagnosis_code'] == 'stateful_cpu_time_limit' and not status['verified']
    assert status['elapsed_s'] >= status['time_limit_s'] == 3600
    assert status['frame_progress'] == {'stage': 'split', 'current_frame': 7, 'frames_per_stage': 140}
    assert status['frames_completed'] is None and not status['gpu_inference_verified']
  assert trial.MEMORY_FLOOR == 2 << 30 and not child.is_alive()
  assert model.read_bytes() == b'original model' and engine.read_bytes() == b'normal engine'
  assert (root / 'current').resolve() == release


def test_unified_diagnosis_keeps_partial_memory_evidence_after_guard_abort(tmp_path):
  from openpilot.tools.jetson.manage import n16_partition_parity_status
  cache = tmp_path / 'cache'
  cache.mkdir()
  memory = {'reference': {'current': {'worker_rss_bytes': 3000000000}, 'phase': 'stateful_cpu_reference_frame_2'}}
  (cache / 'nexo-n16-stateful-cpu-parity-result.json').write_text(json.dumps({
    'state': 'failed', 'worker_result': {'cpu_memory_stages': memory}}))
  result = n16_partition_parity_status(tmp_path)['stateful_cpu_validation']
  assert result['memory_by_stage'] == memory
  assert not result['verified'] and not result['gpu_inference_verified']


def test_bounded_trial_deadlines_and_incomplete_accuracy_diagnosis():
  from openpilot.tools.jetson.nexo_n16_trt_trial import trial_time_limit, trial
  from openpilot.tools.jetson.nexo_n16_diagnostics import classify_trial
  assert trial_time_limit(None, False) == 900
  assert trial_time_limit(1200, False) == 1200
  assert trial_time_limit(None, True) == 3600
  assert trial_time_limit(7200, True) == 7200
  for stateful, budget in ((False, 1201), (True, 7201), (True, 59), (True, float('inf')), (True, float('nan'))):
    with pytest.raises(ValueError, match='Time budget'):
      trial(lambda: pytest.fail('Invalid budget reached device preflight'), timeout_s=budget,
            partition_stateful_cpu_only=stateful)
  legacy = {'state': 'failed', 'error': 'TimeoutError: N16 trial time limit reached', 'partition_stateful_cpu_only': True}
  assert classify_trial(legacy)['diagnosis_code'] == 'stateful_cpu_time_limit'
  assert classify_trial({**legacy, 'state': 'cpu_parity_failed'})['diagnosis_code'] == 'cpu_numerical_parity_failed'


def test_cli_progress_goes_to_stderr_only(capfd):
  from openpilot.tools.jetson.terminal_progress import terminal_progress
  with terminal_progress() as progress:
    progress('N16 build_left')
  output = capfd.readouterr()
  assert output.out == '' and output.err == 'N16 build_left\n'
