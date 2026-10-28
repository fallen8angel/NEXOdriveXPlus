"""Fault injection with real files/TCP and Linux flock; no physical USB claims."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from openpilot.tools.jetson import boot_display, display_owner, hud, protected_ncm, supervisor
from openpilot.tools.jetson.state import atomic_json
from openpilot.selfdrive.modeld.jetlink import display


def test_encoder_wrapped_expiry_is_not_a_usb_failure():
  error = RuntimeError('H264 USB pipeline failed')
  error.__cause__ = hud.SnapshotUnavailable('HUD snapshot expired')
  assert hud.failure_reason(error) == 'snapshot_expired'
  error.__cause__ = hud.RenderExpired('slow capture')
  assert hud.failure_reason(error) == 'render_expired'
  error.__cause__ = OSError('USB disconnected')
  assert hud.failure_reason(error) == 'renderer_error'
  assert hud.failure_reason(RuntimeError('TURZX USB display is busy')) == 'panel_busy'


def test_render_delay_discards_frame_even_when_receiver_is_fresh(monkeypatch):
  now = [100.]
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now[0])
  closed = []
  @contextmanager
  def capture():
    now[0] += .6
    try:
      yield b'old speed', 3, 2
    finally:
      closed.append(True)
  with hud.guarded_capture(capture)() as (raw, width, height):
    assert raw == bytes((0, 0, 0, 255)) * 6 and (width, height) == (3, 2)
  assert closed and hud.RENDER_TIMING['render_ms'] == pytest.approx(600)


def test_boot_mirror_in_landscape_then_portrait():
  frame = Image.new('RGB', (3, 2))
  frame.putdata([(i * 30, 0, 0) for i in range(6)])
  normal = boot_display.panel_frame(frame, 1)
  mirrored = boot_display.panel_frame(frame, 0)
  assert normal.size == mirrored.size == (2, 3)
  assert mirrored.tobytes() == normal.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()


def test_boot_failure_never_resets_usb():
  class Original:
    def _reset_and_reconnect(self):
      pytest.fail('boot must not reset shared USB hardware')
    def _handle_frame_error(self, error):
      self._reset_and_reconnect()
  panel = boot_display.diagnostic_panel(Original)()
  with pytest.raises(RuntimeError, match='without resetting'):
    panel._reset_and_reconnect()
  with pytest.raises(RuntimeError, match='release panel'):
    panel._handle_frame_error(OSError('timeout'))


@pytest.mark.skipif(sys.platform != 'linux', reason='real flock and /proc require Linux')
def test_boot_hud_handoff_and_return_with_real_process(tmp_path):
  code = '''
from pathlib import Path
import sys, time
from openpilot.tools.jetson.display_owner import panel_owner
root = Path(sys.argv[1])
with panel_owner(hud=True, directory=root) as acquired:
  assert acquired
  (root / 'acquired').touch()
  while not (root / 'release').exists(): time.sleep(.01)
'''
  process = None
  try:
    with display_owner.panel_owner(directory=tmp_path) as acquired:
      assert acquired
      process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)])
      deadline = time.monotonic() + 5
      while not display_owner.hud_requested(tmp_path) and time.monotonic() < deadline:
        time.sleep(.01)
      assert display_owner.hud_requested(tmp_path)
      assert not (tmp_path / 'acquired').exists()
    deadline = time.monotonic() + 5
    while not (tmp_path / 'acquired').exists() and time.monotonic() < deadline:
      time.sleep(.01)
    assert (tmp_path / 'acquired').exists()
    with display_owner.panel_owner(directory=tmp_path) as acquired:
      assert not acquired
    (tmp_path / 'release').touch()
    assert process.wait(timeout=5) == 0
    with display_owner.panel_owner(directory=tmp_path) as acquired:
      assert acquired
    assert not display_owner.hud_requested(tmp_path)
  finally:
    if process is not None and process.poll() is None:
      process.kill()
      process.wait(timeout=5)


class Child:
  pid = 123
  returncode = None
  def poll(self):
    return self.returncode


def test_expiry_recovery_and_blocked_rearm_are_bounded(tmp_path, monkeypatch):
  now, wanted, ready, children = [100.], [True], [True], []
  monkeypatch.setattr(supervisor, 'RUNTIME', tmp_path)
  monkeypatch.setattr(supervisor, 'stop_process', lambda *a: True)
  def spawn(*a, **kw):
    children.append(Child())
    return children[-1]
  obj = supervisor.RendererSupervisor([], lambda: wanted[0], lambda: now[0], spawn, lambda: ready[0])
  obj.tick()
  children[-1].returncode = 75
  obj.tick()
  assert obj.failures == 0
  now[0] += 1
  obj.tick()
  assert len(children) == 2
  for _ in range(3):
    children[-1].returncode = 1
    obj.tick()
    now[0] += 30
    obj.tick()
  count = len(children)
  now[0] += 3600
  assert obj.tick()['blocked']
  assert len(children) == count
  wanted[0] = False
  obj.tick()
  wanted[0] = True
  obj.tick()
  now[0] += 2
  assert obj.tick()['state'] == 'running'
  assert len(children) == count + 1
  for _ in range(32):
    now[0] += 1
    atomic_json(tmp_path / 'hud-status.json', {'updated': now[0], 'last_panel_send_mono': now[0], 'pid': 123})
    obj.tick()
  assert obj.consecutive == 0


def test_old_blocked_journal_gets_one_probe_not_permanent_disable(tmp_path, monkeypatch):
  monkeypatch.setattr(supervisor, 'RUNTIME', tmp_path)
  atomic_json(tmp_path / 'hud-recovery.json', {'updated': 100., 'consecutive_failures': 3})
  now = [100.]
  children = []
  obj = supervisor.RendererSupervisor([], lambda: True, lambda: now[0],
                                      lambda *a, **kw: children.append(Child()) or children[-1])
  obj.tick()
  now[0] += 2
  assert obj.tick()['state'] == 'running' and len(children) == 1


def test_data_only_boot_override_and_rollback(tmp_path, monkeypatch):
  root, ram = tmp_path / 'DATA', tmp_path / 'run'
  monkeypatch.setattr(protected_ncm, 'RUN', ram)
  module = root / 'current/openpilot/tools/jetson/boot_display.py'
  module.parent.mkdir(parents=True)
  module.touch()
  calls = []
  monkeypatch.setattr(protected_ncm, 'run', lambda *a, **kw: calls.append(a))
  assert protected_ncm.configure_panel(root)
  assert calls == [('systemctl', 'stop', protected_ncm.BOOT_UNIT)]
  files = protected_ncm.panel_files(root)
  assert all(path.is_relative_to(ram) and path.exists() for path in files)
  assert all('User=jetlink' in path.read_text() for path in files)
  assert all('Environment=NEXO_PANEL_OWNER=1' in path.read_text() for path in files)
  assert '/current/tools/jetlink/boot_display.py' in next(iter(files)).read_text()
  assert not protected_ncm.configure_panel(root)
  module.unlink()  # Selecting the previous release must restore APP's screen.
  assert protected_ncm.configure_panel(root)
  assert all(not path.exists() for path in files)


@pytest.mark.parametrize('standalone', [False, True])
def test_ncm_preparation_budget_is_separate_only_in_display_only(tmp_path, standalone):
  p = display.DisplayPublisher.__new__(display.DisplayPublisher)
  now = [100.]
  p.path, p.status = tmp_path / 'snapshot', {}
  p.next_hud, p.hud_connected, p.last_sent = 0., False, 100.
  p.standalone = standalone
  def packet(stamp):
    now[0] += .010
    return b'fresh'
  p.packet, p.media_packet = packet, lambda: None
  sent = []
  client = SimpleNamespace(last_state={}, _next_seq=lambda: 1,
                           t=SimpleNamespace(send=lambda *a, **kw: sent.append(kw['timeout'])))
  p.send(client, clock=lambda: now[0])
  assert sent == [pytest.approx(.012 if standalone else .002)]
  assert p.status['hud_prepare_ms'] == pytest.approx(10)
