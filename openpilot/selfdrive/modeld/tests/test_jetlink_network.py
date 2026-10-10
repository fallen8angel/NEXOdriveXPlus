import ast
from pathlib import Path
import subprocess

import pytest

from openpilot.selfdrive.modeld.jetlink import network


class FakeParams:
  def __init__(self, **values):
    self.values = {'IsOffroad': True, 'IsOnroad': False, 'NexoJetsonUsb': False, **values}

  def get_bool(self, name):
    return self.values[name]


@pytest.fixture
def usb(tmp_path, monkeypatch):
  root = tmp_path / 'usb0'
  root.mkdir()
  (root / 'flags').write_text('0x1002')
  (root / 'operstate').write_text('down')
  monkeypatch.setattr(network, 'USB0', root)
  monkeypatch.setattr(network, 'enabled', lambda: True)
  monkeypatch.setattr(network.os, 'geteuid', lambda: 1000, raising=False)
  return root


def test_down_is_raised_once_and_repeated_calls_are_noops(usb, monkeypatch):
  commands = []

  def run(args, **kwargs):
    commands.append((args, kwargs))
    (usb / 'flags').write_text('0x1003')

  monkeypatch.setattr(network.subprocess, 'run', run)
  params = FakeParams()
  assert network.ensure_usb0_up(params) == ('up', '')
  assert network.ensure_usb0_up(params) == ('already_up', '')
  assert commands == [(['sudo', '-n', 'ip', 'link', 'set', 'dev', 'usb0', 'up'],
                       {'check': True, 'timeout': 2, 'capture_output': True})]
  # Cable absent/operstate DOWN does not repeatedly raise an admin-UP link.
  assert network.usb0_state()['usb_operstate'] == 'down'


@pytest.mark.parametrize('values', [{'IsOnroad': True}, {'IsOffroad': False}, {'NexoJetsonUsb': True}])
def test_excluded_modes_never_run_network_command(usb, monkeypatch, values):
  monkeypatch.setattr(network.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected mutation'))
  assert network.ensure_usb0_up(FakeParams(**values)) == ('skipped', '')


def test_disabled_marker_and_unknown_params_fail_closed(usb, monkeypatch):
  monkeypatch.setattr(network.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected mutation'))
  monkeypatch.setattr(network, 'enabled', lambda: False)
  assert network.ensure_usb0_up(FakeParams()) == ('skipped', '')
  monkeypatch.setattr(network, 'enabled', lambda: True)
  assert network.ensure_usb0_up(None) == ('skipped', '')
  assert not network.offroad_link_allowed(FakeParams(), started=True)


def test_state_transition_before_command_is_rechecked(usb, monkeypatch):
  params = FakeParams()
  original = network.usb0_state

  def read():
    state = original()
    params.values['IsOnroad'] = True
    return state

  monkeypatch.setattr(network, 'usb0_state', read)
  monkeypatch.setattr(network.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected mutation'))
  assert network.ensure_usb0_up(params) == ('skipped', '')


@pytest.mark.parametrize('error', [PermissionError('denied'), subprocess.TimeoutExpired('ip', 2),
                                 subprocess.CalledProcessError(1, 'ip')])
def test_setup_failures_remain_local_and_retryable(usb, monkeypatch, error):
  def run(*args, **kwargs):
    raise error

  monkeypatch.setattr(network.subprocess, 'run', run)
  action, message = network.ensure_usb0_up(FakeParams())
  assert action == 'error' and message
  monkeypatch.setattr(network.subprocess, 'run', lambda *a, **kw: None)
  assert network.ensure_usb0_up(FakeParams()) == ('up', '')


def test_missing_interface_and_unreadable_admin_flags_are_noops(usb, monkeypatch):
  monkeypatch.setattr(network.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected mutation'))
  (usb / 'flags').unlink()
  assert network.ensure_usb0_up(FakeParams()) == ('skipped', '')
  monkeypatch.setattr(network, 'USB0', usb / 'missing')
  assert network.ensure_usb0_up(FakeParams()) == ('skipped', '')


@pytest.mark.parametrize('flags,operstate,carrier,expected,source', [
  ('0x1002', 'down', '', False, 'admin_down'),
  ('0x11003', 'up', '', True, 'flags_lower_up'),
  ('0x1003', 'down', '', False, 'flags_lower_up'),
  ('', 'up', '', True, 'operstate'),
  ('', 'down', '', False, 'operstate'),
  ('', 'unknown', '', None, 'unknown'),
  ('0x1003', 'unknown', '1', True, 'carrier'),
])
def test_carrier_fallback_preserves_admin_and_physical_distinction(usb, flags, operstate, carrier, expected, source):
  (usb / 'flags').write_text(flags)
  (usb / 'operstate').write_text(operstate)
  (usb / 'carrier').write_text(carrier)
  state = network.usb0_state()
  assert state['usb_carrier'] is expected
  assert state['usb_carrier_source'] == source
  assert state['usb_operstate'] == operstate


def test_manager_registers_optional_tici_offroad_process(usb):
  # Evaluate the real process registration without Linux native dependencies.
  path = Path(__file__).resolve().parents[3] / 'system/manager/process_config.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == 'PythonProcess'
              and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'nexo_jetlink_network')
  args, kwargs = eval(compile(ast.Expression(call), str(path), 'eval'),
                      {'PythonProcess': lambda *a, **kw: (a, kw), 'TICI': True,
                       'offroad_link_allowed': network.offroad_link_allowed})
  assert args[:2] == ('nexo_jetlink_network', 'openpilot.selfdrive.modeld.jetlink.network')
  assert kwargs == {'enabled': True, 'restart_if_crash': True}
  assert args[2](False, FakeParams(), None)
  assert not args[2](True, FakeParams(), None)
  assert not args[2](False, FakeParams(NexoJetsonUsb=True), None)
