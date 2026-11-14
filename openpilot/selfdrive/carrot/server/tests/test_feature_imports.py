"""Independent diagnostics must import without the vehicle's compiled IPC.

Use fresh interpreters and real feature modules: no sys.modules substitutes,
no skipped diagnostics, and no native-module fallback in the server.
"""
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[5]
BLOCK_IPC = '''
import importlib.abc
import sys
class NoVehicleIPC(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path=None, target=None):
    if fullname == 'msgq' or fullname.startswith('msgq.') or fullname == 'openpilot.cereal.messaging':
      raise ModuleNotFoundError('vehicle IPC deliberately unavailable: ' + fullname)
sys.meta_path.insert(0, NoVehicleIPC())
'''


@pytest.mark.parametrize('module', [
  'openpilot.selfdrive.carrot.server.features',
  'openpilot.selfdrive.carrot.server.features.jetson',
])
def test_independent_feature_import_does_not_load_vehicle_ipc(module):
  code = BLOCK_IPC + f'''
import importlib
feature = importlib.import_module({module!r})
assert callable(feature.register_all if {module!r}.endswith('.features') else feature.register)
assert 'openpilot.selfdrive.carrot.server.features.carrot_navi' not in sys.modules
assert 'openpilot.cereal.messaging' not in sys.modules
assert 'msgq' not in sys.modules
'''
  result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr


def test_server_registration_still_requires_native_features():
  code = BLOCK_IPC + '''
from aiohttp import web
from openpilot.selfdrive.carrot.server import features
try:
  features.register_all(web.Application())
except ModuleNotFoundError as error:
  assert 'vehicle IPC deliberately unavailable:' in str(error), str(error)
else:
  raise AssertionError('Server silently omitted a required native feature')
'''
  result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr
