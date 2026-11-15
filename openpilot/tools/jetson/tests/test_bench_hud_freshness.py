"""Every bench frame validates color, plus unmodified real-clock lease semantics."""
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson.bench_hud_local import verify_signal_frame
from openpilot.tools.jetson.hud_signals import RemoteSignals, MAX_AGE
from openpilot.tools.jetson.tests.test_hud_signals import packet


@pytest.mark.parametrize('color', ['red', 'green'])
def test_valid_received_signal_keeps_fresh_and_expired_expectations_separate(color):
  snapshot = packet(raw=color)
  reader = RemoteSignals()
  assert verify_signal_frame(reader, snapshot, color, 10.1) == 'fresh'
  assert verify_signal_frame(reader, snapshot, color, 10. + MAX_AGE + .01) == 'expired'


def test_runner_pause_cannot_make_expired_green_acceptable():
  snapshot = packet(raw='green')
  unsafe = SimpleNamespace(read=lambda value, now: [{'kind': 'green'}])
  with pytest.raises(AssertionError, match='Expired signal remained visible'):
    verify_signal_frame(unsafe, snapshot, 'green', 10.6)


def test_fresh_missing_or_wrong_color_still_fails():
  snapshot = packet(raw='red')
  for lights in ([], [{'kind': 'green'}]):
    broken = SimpleNamespace(read=lambda value, now, lights=lights: lights)
    with pytest.raises(AssertionError, match='Fresh signal missing or wrong color'):
      verify_signal_frame(broken, snapshot, 'red', 10.1)


def test_expired_frame_still_must_pass_received_content_validation():
  snapshot = packet(raw='red')
  with pytest.raises(AssertionError, match='Received signal content'):
    verify_signal_frame(RemoteSignals(), snapshot, 'green', 10.6)
