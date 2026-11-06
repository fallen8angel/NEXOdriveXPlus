"""Jetson HUD wall-time proof without a live panel or Jetson RTC."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openpilot.tools.jetson.hud import hud_clock_text


KST = timezone(timedelta(hours=9))
WALL = datetime(2026, 11, 6, 13, 25, 10, tzinfo=KST).timestamp()


def received(*, wall=WALL, updated=100.0):
  return {'wall_time_unix': wall, 'updated': updated}


def test_uses_trusted_comma_epoch_in_korea_not_jetson_wall_clock():
  # A Jetson rebooted with a 1970 system date still shows the Comma's KST.
  assert hud_clock_text(received(), monotonic_now=100.25) == '13:25:10'
  assert hud_clock_text(received(wall=WALL + 15), monotonic_now=100.3) == '13:25:25'


@pytest.mark.parametrize('value', [None, {}, received(wall=0),
                                  received(wall=float('nan')), received(wall=True),
                                  received(wall='1970'), received(updated=float('inf'))])
def test_missing_or_untrusted_comma_clock_never_displays_false_jetson_time(value, monkeypatch):
  from openpilot.tools.jetson import hud
  monkeypatch.setattr(hud, 'read_snapshot', lambda: value)
  assert hud_clock_text(monotonic_now=100.1) == '--:--:--'


@pytest.mark.parametrize('now', [99.5, 100.5, 104.0])
def test_stale_or_future_snapshot_cannot_be_turned_into_live_clock(now):
  assert hud_clock_text(received(), monotonic_now=now) == '--:--:--'


def test_only_optional_jetson_runner_overrides_standalone_cluster_clock():
  main = Path(__file__).resolve().parents[3] / 'selfdrive/carrot/cluster/main.py'
  tree = ast.parse(main.read_text())
  assert any(isinstance(node, ast.FunctionDef) and node.name == 'current_wall_clock_text'
             for node in tree.body)
  # The Jetson runner binds its display-only clock provider after importing
  # cluster.main. No vehicle process, CAN path or driver monitoring is patched.
  from openpilot.tools.jetson import hud
  assert 'cluster.current_wall_clock_text = hud_clock_text' in Path(hud.__file__).read_text()
