"""Actual native-runtime imports on the built Linux target; no module stubs."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != 'linux', reason='cereal/msgq and the production HUD runtime require the built Linux environment')
@pytest.mark.parametrize('native', [False, True])
def test_real_hud_adapter_and_manager_imports(native):
  root = Path(__file__).resolve().parents[4]
  # A fresh process contains the HUD's process-local adapters. None may leak
  # into manager/tests, and importing must not enumerate/open a camera or HUD.
  if native:
    import os
    server = Path(os.environ.get('CARROT_JETSON', str(Path.home() / 'carrot-jetson')))
    if not (server / 'tools/jetlink/hud_navi.py').is_file():
      pytest.skip('pinned carrot-jetson is required for native HUD adapter')
  code = '''
import sys
from pathlib import Path
from openpilot.system.manager.process_config import managed_processes
assert "nexo_jetson_usb" in managed_processes
assert "nexo_jetson_bridge" in managed_processes
sys.path.insert(0, str(Path("openpilot/selfdrive/carrot/cluster").resolve()))
from openpilot.tools.jetson.hud import install_adapters
from openpilot.selfdrive.modeld.jetlink import daemon
assert callable(daemon.serve_modeld)
NATIVE_SETUP
import main as cluster
assert callable(cluster.main)
'''
  setup = f'sys.path.insert(0, {str(server / "tools/jetlink")!r}); install_adapters(native=True)' if native else 'install_adapters()'
  code = code.replace('NATIVE_SETUP', setup)
  result = subprocess.run([sys.executable, '-c', code], cwd=root, capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr
