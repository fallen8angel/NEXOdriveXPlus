import json
import subprocess
import sys
import threading

from openpilot.tools.jetson import process


def test_real_child_is_reaped_and_duplicate_shutdown_is_safe(tmp_path, monkeypatch):
  monkeypatch.setattr(process, 'RUNTIME', tmp_path)
  child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
  assert process.stop_process(child)
  assert child.poll() is not None
  assert process.stop_process(child)


def test_second_timeout_is_contained_and_only_one_reaper_runs(tmp_path, monkeypatch):
  monkeypatch.setattr(process, 'RUNTIME', tmp_path)
  exit_event = threading.Event()
  calls = []
  class Child:
    def poll(self):
      return 0 if exit_event.is_set() else None
    def terminate(self):
      calls.append('term')
    def kill(self):
      calls.append('kill')
    def wait(self, timeout=None):
      if timeout is not None:
        raise subprocess.TimeoutExpired('camera', timeout)
      calls.append('reap')
      exit_event.wait(2)
  child = Child()
  try:
    assert process.stop_process(child, 'camera') is False
    assert process.stop_process(child, 'camera') is False
    assert calls.count('term') == calls.count('kill') == 1
    assert json.loads((tmp_path / 'camera-shutdown.json').read_text())['stage'] == 'kill_timeout'
  finally:
    exit_event.set()
    for thread in threading.enumerate():
      if thread.name == 'camera-reaper':
        thread.join(2)
    assert id(child) not in process._pending

