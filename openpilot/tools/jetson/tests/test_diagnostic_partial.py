"""Keep real helper output at its deadline, without importing native services."""
import ast
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('output', [b'Jetlink: enabled=False\n', 'Jetlink: enabled=False\n', None])
def test_timeout_keeps_completed_observations_without_claiming_inference_failure(monkeypatch, output):
  path = Path(__file__).resolve().parents[3] / 'selfdrive/carrot/server/features/tools/nexo_can_diag_download.py'
  tree = ast.parse(path.read_text())
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_jetlink_report')
  def run(args, **kwargs):
    assert args == [sys.executable, '-u', 'diagnostic.py'] and kwargs['timeout'] == 5.
    raise subprocess.TimeoutExpired(args, 5., output=output, stderr=b'partial detail')
  monkeypatch.setattr(subprocess, 'run', run)
  namespace = {'subprocess': subprocess, 'sys': sys, 'JETLINK': 'diagnostic.py'}
  exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
  result = namespace['_jetlink_report']()
  if output:
    assert 'Jetlink: enabled=False' in result
  assert 'partial detail' in result and '일부 수집' in result
  assert 'GPU 추론 실패의 증거가 아니며' in result
