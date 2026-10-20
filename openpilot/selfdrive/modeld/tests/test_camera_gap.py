import ast
from pathlib import Path
from types import SimpleNamespace as NS

from openpilot.selfdrive.modeld.camera_gap import CameraGapTrace


class Camera:
  def __init__(self, frames):
    self.frames = iter(frames)
    self.calls = 0
  def recv(self):
    self.calls += 1
    frame = next(self.frames, None)
    if frame is None:
      return None
    self.frame_id, self.timestamp_sof, self.timestamp_eof = frame, frame * 50_000_000, frame * 50_000_000 + 1
    return frame


def test_gap_has_bounded_previous_current_ids_timing_and_jetson_state(monkeypatch):
  calls, clock = [], [10.]
  monkeypatch.setattr('openpilot.selfdrive.modeld.camera_gap.time.monotonic', lambda: clock[0])
  trace = CameraGapTrace(lambda name, **kw: calls.append((name, kw)))
  model = NS(active=True, ready=True, link_state='ready', error='')
  camera = Camera([100, 103])
  for frame, dropped in ((100, 0), (103, 2)):
    trace.begin()
    assert trace.receive(camera, 'main') == frame
    trace.record(frame, dropped, model, inference=20., loop=52.)
  name, record = calls[0]
  assert name == 'modelCameraGap' and record['missing'] == {'first': 101, 'last': 102, 'count': 2}
  assert record['previous']['frame_id'] == 100 and record['current']['frame_id'] == 103
  assert record['current']['jetson_active'] is True and record['current']['jetson_state'] == 'ready'
  assert record['current']['timings_ms'] == {'inference': 20., 'loop': 52.}
  assert record['current']['received'][0]['received_mono'] == 10.
  for frame in range(104, 200):
    trace.record(frame, 1, model)
  assert len(calls) == 1
  clock[0] += 1
  trace.record(201, 1, model)
  assert len(calls) == 2 and calls[-1][1]['previous']['frame_id'] == 199


def test_unbounded_legacy_resync_keeps_only_last_16_observations_and_logger_cannot_raise():
  def fail(*a, **kw):
    raise OSError('log unavailable')
  trace = CameraGapTrace(fail)
  camera = Camera(range(1000))
  for _ in range(1000):
    trace.receive(camera, 'extra')
  assert len(trace.received) == 16 and trace.receive_count == 1000 and camera.calls == 1000
  trace.record(1000, 999, NS())  # No fabricated gap at initial attachment.
  trace.record(2000, 999, NS())
  assert trace.previous['frame_id'] == 2000
  trace.record(1, 0, NS())  # A camera counter reset is retained, not a giant gap.
  assert trace.previous['frame_id'] == 1


def test_actual_nexo_receive_loops_keep_legacy_selection():
  """Execute production selection AST, once traced and once without observation."""
  source = Path(__file__).resolve().parents[1] / 'modeld_local.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  loop = next(n for n in main.body if isinstance(n, ast.While) and any(
    isinstance(c, ast.Attribute) and c.attr == 'begin' for c in ast.walk(n)))
  start = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.While))
  end = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Assign) and any(
    isinstance(t, ast.Name) and t.id == 'camera_ready' for t in n.targets))
  block = '\n'.join(ast.unparse(n) for n in loop.body[start:end])
  assert '25000000' in block and '10000000' in block  # NEXO thresholds, not Carrot's pairing policy.
  plain = block.replace("camera_gap.receive(vipc_client_main, 'main')", 'vipc_client_main.recv()').replace(
    "camera_gap.receive(vipc_client_extra, 'extra')", 'vipc_client_extra.recv()')
  def run(code, extra):
    a, b = Camera([1, 2, 4, 5, 8]), Camera([1, 3, 4, 5, 8])
    env = {'vipc_client_main': a, 'vipc_client_extra': b, 'use_extra_client': extra,
           'meta_main': NS(timestamp_sof=0), 'meta_extra': NS(timestamp_sof=0), 'buf_main': None,
           'FrameMeta': lambda c: NS(frame_id=c.frame_id, timestamp_sof=c.timestamp_sof),
           'cloudlog': NS(debug=lambda *a: None, error=lambda *a: None), 'ids': [], 'camera_gap': CameraGapTrace(lambda *a, **kw: None)}
    exec('for _ in range(6):\n' + '\n'.join('  '+line for line in code.splitlines())
         + '\n  ids.append((meta_main.frame_id, meta_extra.frame_id))', env)
    return env['ids'], a.calls, b.calls
  for extra in (False, True):
    assert run(block, extra) == run(plain, extra)
