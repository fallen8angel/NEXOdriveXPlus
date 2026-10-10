"""Actual read-only HUD adapters on a host without vehicle IPC extensions."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_native_hud_loads_and_decodes_without_vehicle_binaries():
  pytest.importorskip('pyray')
  root = Path(__file__).resolve().parents[4]
  code = '''
import importlib.abc
import sys
from pathlib import Path
class NoVehicleIPC(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path=None, target=None):
    if fullname in ('msgq', 'openpilot.cereal.messaging', 'openpilot.common.params'):
      raise AssertionError('vehicle IPC was imported: ' + fullname)
sys.meta_path.insert(0, NoVehicleIPC())
sys.path.insert(0, str(Path('openpilot/selfdrive/carrot/cluster').resolve()))
from openpilot.tools.jetson.hud import install_adapters, event_from_bytes
install_adapters(native=True)
from openpilot.cereal import log, messaging
event = log.Event.new_message(logMonoTime=123, valid=True)
event.init('can', 1)
event.can[0] = {'address': 0x4F4, 'src': 0, 'dat': b'12345678'}
decoded = event_from_bytes(event.to_bytes())
assert decoded.can[0].dat == b'12345678' and decoded.logMonoTime == 123
assert messaging.log_from_bytes(event.to_bytes()).which() == 'can'
import main as cluster
assert callable(cluster.main)
assert 'msgq' not in sys.modules
'''
  env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)}
  result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env, capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr
