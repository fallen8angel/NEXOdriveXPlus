import itertools
from pathlib import Path
import random
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[4]
CLUSTER = ROOT / 'openpilot/selfdrive/carrot/cluster'
sys.path.insert(0, str(CLUSTER))
from cluster_h264_pipeline import _h264_start_codes, _h264_nals
from cluster_profile import close_session_resources


def reference(data):
  positions, index = [], 0
  while index + 3 <= len(data):
    size = 4 if data[index:index + 4] == b'\0\0\0\1' else 3 if data[index:index + 3] == b'\0\0\1' else 0
    if size:
      positions.append((index, size))
    index += size or 1
  return positions


def test_fast_h264_parser_matches_reference_at_every_short_boundary():
  for length in range(10):
    for values in itertools.product((0, 1, 2), repeat=length):
      data = bytes(values)
      assert _h264_start_codes(data) == reference(data)
  rng = random.Random(8122)
  data = b'\0' + b''.join(b'\0\0\0\1\x65' + rng.randbytes(4091) for _ in range(203)) + b'\0\0'
  assert len(data) > 830000
  assert _h264_start_codes(data) == reference(data)
  for start, payload, end in _h264_nals(data):
    assert data[start:payload] in (b'\0\0\1', b'\0\0\0\1') and payload < end


def test_hud_freeze_restore_and_400_failed_sessions_release_references():
  # A separate interpreter protects pytest's GC state; real profile/recovery
  # functions execute with synthetic USB failures, without any graphics device.
  code = r'''
import gc, sys, weakref
sys.path.insert(0, sys.argv[1])
from cluster_profile import ProfileReporter, freeze_gc_after_init, restore_gc_after_run, release_finished_session_memory
refs = []
initial = gc.get_threshold()
initial_frozen = gc.get_freeze_count()
class Cycle: pass
def session():
    value = Cycle()
    value.self = value
    refs.append(weakref.ref(value))
    previous = freeze_gc_after_init(ProfileReporter(False, 1))
    try:
        raise ConnectionError('panel removed')
    finally:
        restore_gc_after_run(previous)
for index in range(400):
    try: session()
    except ConnectionError: pass
    release_finished_session_memory()
    assert all(ref() is None for ref in refs), (index, [ref() for ref in refs if ref() is not None])
    assert gc.get_threshold() == initial and gc.get_freeze_count() <= initial_frozen, (initial, gc.get_threshold(), gc.get_freeze_count())
'''
  result = subprocess.run([sys.executable, '-c', code, str(CLUSTER)], capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr


def test_cleanup_continues_after_disconnected_device_close_error():
  closed = []
  def broken():
    closed.append('encoder')
    raise OSError('panel removed')
  close_session_resources([broken, lambda: closed.append('USB'), lambda: closed.append('renderer')])
  assert closed == ['encoder', 'USB', 'renderer']
