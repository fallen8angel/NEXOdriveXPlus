"""Actual native-runtime imports on the built Linux target; no module stubs."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != 'linux', reason='cereal/msgq and the production HUD runtime require the built Linux environment')
def test_real_hud_adapter_and_manager_imports():
  root = Path(__file__).resolve().parents[4]
  # A fresh process contains the HUD's process-local adapters. None may leak
  # into manager/tests, and importing must not enumerate/open a camera or HUD.
  code = '''
import sys
from pathlib import Path
from openpilot.system.manager.process_config import managed_processes
assert "nexo_jetson_usb" in managed_processes
assert "nexo_jetson_bridge" in managed_processes
sys.path.insert(0, str(Path("openpilot/selfdrive/carrot/cluster").resolve()))
from openpilot.tools.jetson.hud import install_adapters
install_adapters()
import main as cluster
assert callable(cluster.main)
'''
  result = subprocess.run([sys.executable, '-c', code], cwd=root, capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr
