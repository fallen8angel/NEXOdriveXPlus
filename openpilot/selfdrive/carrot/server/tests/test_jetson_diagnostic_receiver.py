import importlib.util
from pathlib import Path
import socket

import pytest


@pytest.fixture
def diagnostic(monkeypatch):
  path = Path(__file__).resolve().parents[1] / 'features/tools/nexo_jetson_diag.py'
  spec = importlib.util.spec_from_file_location('jetson_diagnostic_receiver_test', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, 'OBSERVE_SECONDS', .01)
  monkeypatch.setattr(module, '_init_local_stream_probe', lambda: (None, ''))
  for name in ('_sample_local_stream', '_print_local_camera_snapshot', 'print_boot_diagnostic', 'print_remote_update_diagnostic'):
    monkeypatch.setattr(module, name, lambda *a: None)
  return module


def test_diagnostic_reads_shared_web_receiver_without_second_bind(diagnostic, monkeypatch, capsys):
  monkeypatch.setattr(diagnostic, '_web_status', lambda: {'ok': True, 'receiver_active': True, 'stages': []})
  monkeypatch.setattr(diagnostic.socket, 'socket', lambda *a, **kw: pytest.fail('duplicate UDP listener'))
  from openpilot.selfdrive.carrot.server.services import jetson
  monkeypatch.setattr(jetson, 'diagnostic_summary', lambda value: 'JETLINK: WAITING')
  assert diagnostic.main() == 0
  out = capsys.readouterr().out
  assert '추가 bind 없음' in out and 'JETLINK: WAITING' in out


def test_actual_port_conflict_is_unobservable_not_offline(diagnostic, monkeypatch, capsys):
  blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    blocker.bind(('0.0.0.0', 0))
    monkeypatch.setattr(diagnostic, 'STATUS_PORT', blocker.getsockname()[1])
    monkeypatch.setattr(diagnostic, '_web_status', lambda: None)
    assert diagnostic.main() == 0
    out = capsys.readouterr().out
    assert '[진단 불가]' in out and '[미연결]' not in out
    assert blocker.getsockname()[1] == diagnostic.STATUS_PORT
  finally:
    blocker.close()
