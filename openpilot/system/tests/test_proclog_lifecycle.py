"""Exercise real cache publication against process churn, without Linux IPC."""
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
  for name, attrs in {
    'openpilot.cereal.messaging': {},
    'openpilot.common.realtime': {'Ratekeeper': object},
    'openpilot.common.swaglog': {'cloudlog': SimpleNamespace(exception=lambda *args: None)},
  }.items():
    fake = ModuleType(name)
    fake.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, fake)
  import openpilot.cereal
  monkeypatch.setattr(openpilot.cereal, 'messaging', sys.modules['openpilot.cereal.messaging'], raising=False)
  monkeypatch.setattr(os, 'sysconf_names', {'SC_CLK_TCK': 1, 'SC_PAGE_SIZE': 2}, raising=False)
  monkeypatch.setattr(os, 'sysconf', lambda name: 100 if name == 1 else 4096, raising=False)
  spec = importlib.util.spec_from_file_location('tested_proclogd', Path(__file__).parents[1] / 'proclogd.py')
  value = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(value)
  return value


def stat(pid, start=10, rss=2000):
  return dict(pid=pid, starttime=start, rss=rss, name='helper', state='S', ppid=1,
              utime=0, stime=0, cutime=0, cstime=0, priority=20, nice=0, num_threads=1, vms=1, processor=0)


def test_both_caches_remain_bounded_during_2000_process_generations(module, monkeypatch):
  monkeypatch.setattr(module.os, 'readlink', lambda _: '/bin/helper')
  monkeypatch.setattr('builtins.open', lambda *args, **kwargs: io.BytesIO(b'helper\0argument\0'))
  stable = module._get_proc_extra(1, 'helper', 10)
  for generation in range(2000):
    pid = generation + 2
    module._prune_proc_caches([stat(1), stat(pid, generation)])
    module._get_proc_extra(pid, 'helper', generation)
    module._smaps_cache[pid] = {'pss': 1, 'pss_anon': 1, 'pss_shmem': 0}
    assert len(module._proc_cache) == 2 and len(module._smaps_cache) == 1
  module._prune_proc_caches([stat(1)])
  assert module._proc_cache == {1: stable} and not module._smaps_cache


def test_same_name_pid_reuse_and_shrinking_rss_expire_stale_metadata(module, monkeypatch):
  command = [b'old\0']
  monkeypatch.setattr(module.os, 'readlink', lambda _: '/bin/helper')
  monkeypatch.setattr('builtins.open', lambda *args, **kwargs: io.BytesIO(command[0]))
  assert module._get_proc_extra(1, 'helper', 10)['cmdline'] == ['old']
  module._smaps_cache[1] = {'pss': 999, 'pss_anon': 999, 'pss_shmem': 0}
  module._prune_proc_caches([stat(1, 11)])
  command[0] = b'new\0'
  assert module._get_proc_extra(1, 'helper', 11)['cmdline'] == ['new']
  assert not module._smaps_cache
  module._smaps_cache[1] = {'pss': 999, 'pss_anon': 999, 'pss_shmem': 0}
  module._prune_proc_caches([stat(1, 11, rss=1)])
  assert not module._smaps_cache


def test_actual_publication_prunes_exited_processes(module, monkeypatch):
  from openpilot.cereal import log
  processes = [stat(1)]
  monkeypatch.setattr(module, '_procs', lambda: processes)
  monkeypatch.setattr(module.os, 'readlink', lambda _: '/bin/helper')
  monkeypatch.setattr('builtins.open', lambda *args, **kwargs: io.BytesIO(b'helper\0'))
  monkeypatch.setattr(module, '_cpu_times', list)
  monkeypatch.setattr(module, '_mem_info', lambda: dict.fromkeys(
    ['MemTotal:', 'MemFree:', 'MemAvailable:', 'Buffers:', 'Cached:', 'Active:', 'Inactive:', 'Shmem:'], 0))
  monkeypatch.setattr(module, '_read_smaps', lambda _: {'pss': 5, 'pss_anon': 3, 'pss_shmem': 2})
  event = log.Event.new_message(procLog={})
  module.build_proc_log_message(event)
  assert list(event.procLog.procs[0].cmdline) == ['helper']
  assert event.procLog.procs[0].memPss == 5
  processes.clear()
  empty = log.Event.new_message(procLog={})
  module.build_proc_log_message(empty)
  assert len(empty.procLog.procs) == 0
  assert not module._proc_cache and not module._smaps_cache
