"""Read-only existing-key diagnostics; no actual SSH or vehicle I/O."""
import base64
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS

import pytest

from openpilot.tools.jetson import ssh_preflight as ssh


@pytest.mark.parametrize('error,code', [
  ('Permission denied (publickey).', 'SSH_AUTH_DENIED'),
  ('REMOTE HOST IDENTIFICATION HAS CHANGED!', 'SSH_HOST_KEY_CHANGED'),
  ('Host key verification failed.', 'SSH_HOST_KEY_UNKNOWN'),
  ('No ED25519 host key is known for host', 'SSH_HOST_KEY_UNKNOWN'),
  ('UNPROTECTED PRIVATE KEY FILE! bad permissions', 'SSH_KEY_UNREADABLE'),
  ('Load key: invalid format', 'SSH_KEY_INVALID'),
  ('connect to host: Connection refused', 'SSH_CONNECTION_REFUSED'),
  ('No route to host', 'SSH_NETWORK_UNREACHABLE'),
  ('Connection timed out', 'SSH_TIMEOUT'),
  ('unknown failure', 'SSH_REMOTE_COMMAND_FAILED'),
])
def test_exit_255_is_not_assumed_to_be_authentication(error, code):
  value = ssh.classify_failure(error + '\nPRIVATE SECRET BANNER', 255)
  assert value['code'] == code and value['settings_changed'] is False
  assert 'SECRET' not in json.dumps(value) and 'PRIVATE' not in json.dumps(value)


@pytest.fixture
def existing_keys(tmp_path, monkeypatch):
  directory = tmp_path / '.ssh'
  directory.mkdir()
  private = directory / 'id_ed25519'
  private.write_text('TEST PRIVATE CONTENT MUST NEVER BE COLLECTED')
  private.chmod(0o600)
  public = directory / 'id_ed25519.pub'
  public.write_text('ssh-ed25519 ' + base64.b64encode(b'synthetic public key').decode())
  known = directory / 'known_hosts'
  known.write_text('existing trusted hosts are preserved')
  configured = '\n'.join(['hostname ' + ssh.COMMA, 'user comma', 'identityfile ' + str(private),
                           'userknownhostsfile ' + str(known)])
  calls = []
  state = {'returncode': 0, 'stderr': '', 'response': {'IsOffroad': '1', 'IsOnroad': '0', 'JetlinkEnabled': False},
           'configured': configured}
  def run(args, **kwargs):
    calls.append((args, kwargs))
    assert 'StrictHostKeyChecking=yes' in args and 'BatchMode=yes' in args
    if '-G' in args:
      return NS(returncode=0, stderr='', stdout=state['configured'])
    assert kwargs['input'] == ssh.OFFROAD_SCRIPT and args[-2:] == ['python3', '-']
    return NS(returncode=state['returncode'], stderr=state['stderr'], stdout=json.dumps(state['response']))
  monkeypatch.setattr(ssh.subprocess, 'run', run)
  monkeypatch.setattr(ssh, 'ncm_check', lambda *a: {'ok': True, 'interface': 'enxusb'})
  return private, public, known, state, calls


def test_existing_identity_and_known_hosts_are_preserved_and_private_bytes_not_read(existing_keys, monkeypatch):
  private, public, known, state, calls = existing_keys
  before = {p: p.read_bytes() for p in (private, public, known)}
  original = Path.read_text
  def read(path, *args, **kwargs):
    assert path != private, 'attempted private-key collection'
    return original(path, *args, **kwargs)
  monkeypatch.setattr(Path, 'read_text', read)
  result = ssh.probe('comma@' + ssh.COMMA, str(private))
  assert result['ok'] and result['offroad_allowed'] and result['public_key_registration_verified']
  assert str(private) in calls[-1][0] and 'IdentitiesOnly=yes' in calls[-1][0]
  assert 'TEST PRIVATE' not in json.dumps(result)
  assert result['client']['identity_files'][0]['public_key']['fingerprints']
  assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('offroad,onroad,join', [('0','1',False),('1','1',False),('0','0',False),('1','0',True)])
def test_ssh_success_does_not_override_offroad_guard(existing_keys, offroad, onroad, join):
  private, _, _, state, _ = existing_keys
  state['response'] = {'IsOffroad': offroad, 'IsOnroad': onroad, 'JetlinkEnabled': join}
  value = ssh.probe('comma@' + ssh.COMMA, str(private))
  assert value['authentication_verified'] is True and value['offroad_allowed'] is False


def test_reverse_denied_is_distinct_from_forward_success_and_is_recorded(existing_keys, tmp_path):
  private, _, _, state, _ = existing_keys
  state.update(returncode=255, stderr='Permission denied (publickey).')
  guard = ssh.CommaGuard('comma@' + ssh.COMMA, str(private), root=tmp_path)
  with pytest.raises(ssh.SSHFailure, match='SSH_AUTH_DENIED'):
    guard()
  value = json.loads((tmp_path / 'cache/nexo-comma-ssh-preflight.json').read_text())
  assert value['direction'] == 'jetson_to_comma' and value['ok'] is False
  assert value['code'] == 'SSH_AUTH_DENIED' and 'TEST PRIVATE' not in json.dumps(value)


def test_missing_or_insecure_selected_key_fails_before_connect(existing_keys):
  private, _, _, _, calls = existing_keys
  private.chmod(0o644)
  result = ssh.probe('comma@' + ssh.COMMA, str(private))
  assert result['code'] == 'SSH_KEY_UNREADABLE' and len(calls) == 1


def test_network_check_failure_prevents_ssh_and_gpu_test(existing_keys, monkeypatch):
  _, _, _, _, calls = existing_keys
  def missing(*args):
    raise ssh.SSHFailure({'ok': False, 'code': 'USB_NCM_UNAVAILABLE', 'recommendation': 'check cable'})
  monkeypatch.setattr(ssh, 'ncm_check', missing)
  result = ssh.probe('comma@' + ssh.COMMA)
  assert result['code'] == 'USB_NCM_UNAVAILABLE' and len(calls) == 1


def test_timeout_and_invalid_remote_state_fail_closed(existing_keys, monkeypatch):
  _, _, _, state, _ = existing_keys
  state['response'] = {'IsOffroad': '1', 'IsOnroad': '0', 'JetlinkEnabled': None}
  assert ssh.probe('comma@' + ssh.COMMA)['code'] == 'SSH_REMOTE_RESPONSE_INVALID'
  monkeypatch.setattr(ssh, 'inventory', lambda *a: {})
  monkeypatch.setattr(ssh.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('ssh', 7)))
  assert ssh.probe('comma@' + ssh.COMMA)['code'] == 'SSH_TIMEOUT'


@pytest.mark.parametrize('target', ['root@10.0.0.1','-oProxyCommand=evil','comma@192.168.100.127;id', 'jetlink@192.168.100.250'])
def test_fixed_comma_target_is_not_replaceable(target):
  with pytest.raises(ValueError):
    ssh.CommaGuard(target)
