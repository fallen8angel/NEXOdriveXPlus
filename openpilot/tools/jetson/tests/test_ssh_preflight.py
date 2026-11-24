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
  configured = '\n'.join(['hostname ' + ssh.COMMA, 'user comma', 'port 22', 'identityfile ' + str(private),
                           'userknownhostsfile ' + str(known)])
  calls = []
  state = {'returncode': 0, 'stderr': '', 'response': {'IsOffroad': '1', 'IsOnroad': '0', 'JetlinkEnabled': False},
           'configured': configured}
  def run(args, **kwargs):
    calls.append((args, kwargs))
    assert 'StrictHostKeyChecking=yes' in args and 'BatchMode=yes' in args
    if '-G' in args:
      return NS(returncode=0, stderr='', stdout=state['configured'])
    if args[-3:] == ['python3', '-S', '-']:
      assert kwargs['input'] == ssh.STATE_SCRIPT + ssh.STATE_RESPONSE
    else:
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


def test_reverse_ncm_route_failure_reports_safe_reason(monkeypatch):
  def fail(args, **kwargs):
    assert args[:3] == ['ip', '-j', '-4']
    raise subprocess.CalledProcessError(2, args, stderr='PRIVATE DEVICE DETAILS')
  monkeypatch.setattr(ssh.subprocess, 'run', fail)
  with pytest.raises(ssh.SSHFailure) as exc:
    ssh.ncm_check('comma@' + ssh.COMMA)
  evidence = exc.value.diagnosis
  assert evidence['code'] == 'USB_NCM_UNAVAILABLE'
  assert evidence['ncm_reason'] == 'ROUTE_LOOKUP_FAILED'
  assert evidence['settings_changed'] is False
  assert 'PRIVATE DEVICE DETAILS' not in json.dumps(evidence)


def test_reverse_ncm_wrong_route_is_not_mistaken_for_auth_error(monkeypatch):
  monkeypatch.setattr(ssh.subprocess, 'run', lambda *a, **k:
    NS(stdout=json.dumps([{'gateway': '192.168.1.1', 'dev': 'wlan0', 'src': ssh.JETSON}])))
  with pytest.raises(ssh.SSHFailure) as exc:
    ssh.ncm_check('comma@' + ssh.COMMA)
  assert exc.value.diagnosis['ncm_reason'] == 'ROUTE_NOT_DIRECT'
  assert exc.value.diagnosis['code'] == 'USB_NCM_UNAVAILABLE'


def test_bad_direct_route_preserves_observed_interface_for_diagnosis(monkeypatch):
  route = [{'dev': 'wlan0', 'src': ssh.JETSON, 'gateway': '192.168.1.1'}]
  monkeypatch.setattr(ssh.subprocess, 'run', lambda *a, **k: NS(stdout=json.dumps(route)))
  with pytest.raises(ssh.SSHFailure) as exc:
    ssh.ncm_check('comma@' + ssh.COMMA)
  value = exc.value.diagnosis
  assert value['ncm_reason'] == 'ROUTE_NOT_DIRECT'
  assert value['route_observed']['dev'] == 'wlan0'
  assert value['expected_source'] == ssh.JETSON
  assert value['settings_changed'] is False


def test_missing_existing_credentials_are_reported_without_creating_keys(existing_keys, monkeypatch):
  private, public, known, state, _ = existing_keys
  monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
  private.unlink()
  public.unlink()
  known.unlink()
  result = ssh.probe('comma@' + ssh.COMMA)
  assert result['credential_preflight']['identity_candidate_present'] is False
  assert result['credential_preflight']['known_hosts_present'] is False
  assert result['credential_preflight']['settings_changed'] is False
  assert not private.exists() and not public.exists() and not known.exists()

def test_direct_enx_usb_route_without_src_uses_interface_address_proof(monkeypatch, tmp_path):
  # Reproduces the real Jetson route: dev=enx..., no src/prefsrc field.
  iface = 'enx9ac49dd41740'
  base = tmp_path / 'sys/class/net' / iface
  base.mkdir(parents=True)
  (base / 'carrier').write_text('1')
  driver = tmp_path / 'drivers/cdc_ncm'
  driver.mkdir(parents=True)
  (base / 'device').mkdir()
  (base / 'device/driver').symlink_to(driver, target_is_directory=True)
  monkeypatch.setattr(ssh, 'Path', lambda value: tmp_path / str(value).lstrip('/'))
  calls = []
  def run(args, **_kwargs):
    calls.append(args)
    if 'route' in args:
      return NS(stdout=json.dumps([{'dev': iface}]))
    return NS(stdout=json.dumps([{'ifname': iface, 'addr_info': [
      {'family': 'inet', 'local': ssh.JETSON, 'prefixlen': 24}]}]))
  monkeypatch.setattr(ssh.subprocess, 'run', run)
  result = ssh.ncm_check('comma@' + ssh.COMMA)
  assert result['ok'] and result['interface'] == iface
  assert result['source_verification'] == 'interface_address'
  assert len(calls) == 2 and 'addr' in calls[1]


def test_direct_route_without_source_or_local_address_remains_blocked(monkeypatch, tmp_path):
  iface = 'enx9ac49dd41740'
  base = tmp_path / 'sys/class/net' / iface
  base.mkdir(parents=True)
  (base / 'carrier').write_text('1')
  driver = tmp_path / 'drivers/cdc_ncm'
  driver.mkdir(parents=True)
  (base / 'device').mkdir()
  (base / 'device/driver').symlink_to(driver, target_is_directory=True)
  monkeypatch.setattr(ssh, 'Path', lambda value: tmp_path / str(value).lstrip('/'))
  monkeypatch.setattr(ssh.subprocess, 'run', lambda args, **kwargs: NS(stdout=json.dumps(
    [{'dev': iface}] if 'route' in args else [{'ifname': iface, 'addr_info': [
      {'family': 'inet', 'local': '192.168.100.251'}]}])))
  with pytest.raises(ssh.SSHFailure) as exc:
    ssh.ncm_check('comma@' + ssh.COMMA)
  assert exc.value.diagnosis['ncm_reason'] == 'SOURCE_ADDRESS_UNVERIFIED'
  assert not exc.value.diagnosis['settings_changed']



def test_host_key_preview_never_trusts_ssh_keyscan_alone(monkeypatch):
  public = 'ssh-ed25519 ' + base64.b64encode(b'example-ed25519-public-key-that-is-not-private-material').decode()
  observed = ssh.COMMA + ' ' + public
  monkeypatch.setattr(ssh, 'ncm_check', lambda target: {'ok': True, 'interface': 'enxusb'})
  def scan(args, **kwargs):
    assert args[:2] == ['ssh-keyscan', '-T'] and args[-1] == ssh.COMMA
    return NS(returncode=0, stdout=observed + '\n', stderr='')
  monkeypatch.setattr(ssh.subprocess, 'run', scan)
  value = ssh.comma_host_key_preview('comma@' + ssh.COMMA)
  assert value['ok'] and value['trusted'] is False and value['settings_changed'] is False
  assert value['candidate_fingerprint'] == ssh.fingerprints(observed)[0]
  assert 'public-key' not in json.dumps(value)


def test_operator_confirmed_host_key_registration_is_bounded_and_idempotent(monkeypatch, tmp_path):
  public = 'ssh-ed25519 ' + base64.b64encode(b'example-ed25519-public-key-that-is-not-private-material').decode()
  observed = ssh.COMMA + ' ' + public
  fp = ssh.fingerprints(observed)[0]
  directory = tmp_path / '.ssh'
  directory.mkdir(mode=0o700)
  known = directory / 'known_hosts'
  known.write_text('example.org ssh-ed25519 ' + base64.b64encode(b'other-valid-public-key-material-length').decode() + '\n')
  known.chmod(0o600)
  monkeypatch.setattr(ssh.Path, 'home', classmethod(lambda cls: tmp_path))
  monkeypatch.setattr(ssh, 'ncm_check', lambda target: {'ok': True, 'interface': 'enxusb'})
  monkeypatch.setattr(ssh, 'inventory', lambda target: {'known_hosts': [{'path': str(known)}]})
  def scan(args, **kwargs):
    if args[0] == 'ssh-keyscan':
      return NS(returncode=0, stdout=observed + '\n', stderr='')
    assert args[0] == 'ssh-keygen' and args[1] == '-F'
    recorded = known.read_text()
    return NS(returncode=0 if ssh.COMMA in recorded else 1,
              stdout=observed + '\n' if ssh.COMMA in recorded else '', stderr='')
  monkeypatch.setattr(ssh.subprocess, 'run', scan)
  assert ssh.enroll_comma_host_key('comma@' + ssh.COMMA, 'SHA256:' + 'A'*43)['code'] == 'SSH_HOST_KEY_MISMATCH'
  assert observed not in known.read_text()
  registered = ssh.enroll_comma_host_key('comma@' + ssh.COMMA, fp)
  assert registered['ok'] and registered['settings_changed'] and observed in known.read_text()
  assert 'example.org' in known.read_text() and (known.stat().st_mode & 0o777) == 0o600
  before = known.read_bytes()
  repeated = ssh.enroll_comma_host_key('comma@' + ssh.COMMA, fp)
  assert repeated['code'] == 'SSH_HOST_ALREADY_TRUSTED'
  assert repeated['settings_changed'] is False and known.read_bytes() == before


def test_host_key_enrollment_refuses_preexisting_conflicting_trust(monkeypatch, tmp_path):
  observed = ssh.COMMA + ' ssh-ed25519 ' + base64.b64encode(b'example-public-key-material-xxxxxxxxxx').decode()
  fp = ssh.fingerprints(observed)[0]
  directory = tmp_path / '.ssh'
  directory.mkdir(mode=0o700)
  known = directory / 'known_hosts'
  known.write_text(ssh.COMMA + ' ssh-ed25519 ' + base64.b64encode(b'different-public-key-material-xxx').decode() + '\n')
  known.chmod(0o600)
  monkeypatch.setattr(ssh.Path, 'home', classmethod(lambda cls: tmp_path))
  monkeypatch.setattr(ssh, 'ncm_check', lambda target: {'ok': True})
  monkeypatch.setattr(ssh, 'inventory', lambda target: {'known_hosts': [{'path': str(known)}]})
  monkeypatch.setattr(ssh.subprocess, 'run', lambda args, **kwargs:
      NS(returncode=0, stdout=observed+'\n' if args[0]=='ssh-keyscan' else known.read_text(), stderr=''))
  before = known.read_bytes()
  result = ssh.enroll_comma_host_key('comma@' + ssh.COMMA, fp)
  assert result['ok'] is False and result['settings_changed'] is False
  assert known.read_bytes() == before


def test_n16_poll_reads_fresh_state_and_preserves_security(existing_keys, tmp_path, monkeypatch):
  private, _, _, state, calls = existing_keys
  routes = []
  monkeypatch.setattr(ssh, 'ncm_check', lambda target: routes.append(target) or {'ok': True})
  guard = ssh.CommaGuard('comma@' + ssh.COMMA, str(private), tmp_path)
  assert guard.live_state() and guard.last['check_kind'] == 'preflight'
  monkeypatch.setattr(ssh, 'inventory', lambda *a: pytest.fail('repeated credential inventory'))
  assert guard.live_state() and guard.last['check_kind'] == 'live_state'
  state['response']['JetlinkEnabled'] = True
  assert not guard.live_state()
  reads = [(args, kwargs) for args, kwargs in calls if '-G' not in args]
  assert len(reads) == len(routes) == 3
  assert len([args for args, _ in calls if '-G' in args]) == 1
  for args, _ in reads[1:]:
    assert 'Hostname=' + ssh.COMMA in args and args[args.index('-b') + 1] == ssh.JETSON
    assert args[args.index('-p') + 1] == '22' and 'ProxyCommand=none' in args and 'ProxyJump=none' in args
  for args, kwargs in reads:
    assert 'StrictHostKeyChecking=yes' in args and 'ConnectionAttempts=1' in args
    assert args[args.index('-S') + 1] == 'none' and kwargs['timeout'] == 7
  assert 'authorized_keys' in reads[0][1]['input'] and 'authorized_keys' not in reads[1][1]['input']


@pytest.mark.parametrize('change,code', [('key', 'SSH_KEY_UNREADABLE'),
                                         ('host', 'SSH_HOST_KEY_CHANGED'), ('json', 'SSH_REMOTE_RESPONSE_INVALID')])
def test_n16_poll_refuses_changed_trust_or_unknown_state(existing_keys, tmp_path, change, code):
  private, _, _, state, _ = existing_keys
  guard = ssh.CommaGuard('comma@' + ssh.COMMA, str(private), tmp_path)
  assert guard()
  if change == 'key':
    private.chmod(0o644)
  elif change == 'host':
    state.update(returncode=255, stderr='REMOTE HOST IDENTIFICATION HAS CHANGED!')
  else:
    state['response'].pop('IsOffroad')
  with pytest.raises(ssh.SSHFailure, match=code):
    guard.live_state()


@pytest.mark.parametrize('partial,step', [(None, 'ssh_before_remote_marker'), (b'Connection timed out during banner exchange', 'ssh_banner_exchange'),
                                          (b'NEXO_GUARD IsOffroad\nNEXO_GUARD IsOnroad\nsecret', 'remote_IsOnroad')])
def test_n16_poll_timeout_records_last_marker_without_raw_stderr(existing_keys, tmp_path, monkeypatch, partial, step):
  private, _, _, _, _ = existing_keys
  guard = ssh.CommaGuard('comma@' + ssh.COMMA, str(private), tmp_path)
  assert guard()
  run = ssh.subprocess.run
  def timeout(args, **kwargs):
    if '-G' in args:
      return run(args, **kwargs)
    raise subprocess.TimeoutExpired('ssh', 7, stderr=partial)
  monkeypatch.setattr(ssh.subprocess, 'run', timeout)
  with pytest.raises(ssh.SSHFailure, match='SSH_TIMEOUT'):
    guard.live_state()
  result = json.loads((tmp_path / 'cache/nexo-comma-ssh-preflight.json').read_text())
  assert result['stage'] == 'ssh_command' and result['remote_step'] == step
  assert result['elapsed_ms'] >= result['stage_started_ms'] >= 0 and result['timeout_s'] == 7
  assert 'secret' not in json.dumps(result)


def test_real_periodic_script_preserves_marker_and_params_semantics(tmp_path):
  import sys
  params = tmp_path / 'params'
  params.mkdir()
  (params / 'IsOffroad').write_text('1')
  (params / 'IsOnroad').write_text('0')
  marker = tmp_path / 'enabled'
  script = (ssh.STATE_SCRIPT + ssh.STATE_RESPONSE).replace('/data/params/d/', str(params) + '/').replace('/data/nexo_jetlink_enabled', str(marker))
  def run():
    return subprocess.run([sys.executable, '-S', '-'], input=script, capture_output=True, text=True, timeout=7)
  result = run()
  assert result.returncode == 0
  assert json.loads(result.stdout) == {'IsOffroad': '1', 'IsOnroad': '0', 'JetlinkEnabled': False}
  assert ssh.ssh_step(result.stderr) == 'remote_response'
  marker.symlink_to(tmp_path / 'absent')
  assert json.loads(run().stdout)['JetlinkEnabled']
  (params / 'IsOnroad').unlink()
  result = run()
  assert result.returncode and not result.stdout and ssh.ssh_step(result.stderr) == 'remote_IsOnroad'


def test_initial_config_still_rejects_proxy(existing_keys, tmp_path):
  private, _, _, state, calls = existing_keys
  state['configured'] += '\nproxycommand changed'
  guard = ssh.CommaGuard('comma@' + ssh.COMMA, str(private), tmp_path)
  with pytest.raises(ssh.SSHFailure, match='SSH_CONFIG_INVALID'):
    guard.live_state()
  assert len(calls) == 1 and '-G' in calls[0][0]


def test_actual_openssh_poll_pins_route_and_honors_configured_trust(existing_keys, monkeypatch, tmp_path):
  private, _, known, state, calls = existing_keys
  state['configured'] = state['configured'].replace('port 22', 'port 2222')
  guard = ssh.CommaGuard('comma@' + ssh.COMMA, str(private), tmp_path)
  assert guard()
  monkeypatch.setattr(ssh, 'checked_config', lambda *a: pytest.fail('live poll launched ssh -G'))
  assert guard.live_state()
  args = calls[-1][0][:-3]  # Inspect the real command with OpenSSH, without opening a socket.
  monkeypatch.undo()
  config = tmp_path / 'config'
  config.write_text(f'''Host *
  HostName 127.0.0.2
  Port 23
  ProxyJump other
  User other
  StrictHostKeyChecking no
  UserKnownHostsFile "{known}"
''')
  observed = subprocess.run(['ssh', '-F', str(config), '-G', *args[1:]], capture_output=True, text=True, timeout=4, check=True)
  fields = dict(line.partition(' ')[::2] for line in observed.stdout.splitlines())
  assert fields['hostname'] == ssh.COMMA and fields['user'] == 'comma' and fields['port'] == '2222'
  assert fields['bindaddress'] == ssh.JETSON and fields.get('proxyjump', 'none') == fields.get('proxycommand', 'none') == 'none'
  assert fields['stricthostkeychecking'] in ('true', 'yes') and fields['userknownhostsfile'] == str(known)
