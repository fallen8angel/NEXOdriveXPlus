"""Real RAM/DATA files and network state machine; Linux mutations are doubles."""
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

from openpilot.tools.jetson import protected_ncm as ncm
from openpilot.tools.jetson.protected_probe import validate_outputs

IFACE = 'enx36770f430ea2'
SHA = 'f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5'


@pytest.fixture
def linux(tmp_path, monkeypatch):
  net, ram = tmp_path / 'sys/net', tmp_path / 'run'
  net.mkdir(parents=True)
  ram.mkdir()
  monkeypatch.setattr(ncm, 'NET', net)
  monkeypatch.setattr(ncm, 'RUN', ram)
  state = {'names': {}, 'routes': [], 'commands': [], 'remove_on_unmanage': False}
  def add(name=IFACE, index=7, vendor='04d8', product='1234', driver='cdc_ncm'):
    path = net / name
    path.mkdir()
    (path / 'carrier').write_text('1')
    usb = tmp_path / f'usb/{name}'
    usb.mkdir(parents=True, exist_ok=True)
    (usb / 'idVendor').write_text(vendor)
    (usb / 'idProduct').write_text(product)
    state['names'][name] = {'index': index, 'usb': usb, 'driver': driver, 'managed': 'yes', 'up': True, 'addr': []}
  add()
  resolve = Path.resolve
  def path_resolve(path, *a, **kw):
    if path.name == 'driver' and path.parent.name == 'device':
      return Path('drivers') / state['names'][path.parent.parent.name]['driver']
    if path.name == 'device' and path.parent.name in state['names']:
      return state['names'][path.parent.name]['usb'] / '1-2:1.0'
    return resolve(path, *a, **kw)
  monkeypatch.setattr(Path, 'resolve', path_resolve)
  def index(name):
    if name not in state['names']:
      raise OSError('No such device')
    return state['names'][name]['index']
  monkeypatch.setattr(ncm.socket, 'if_nametoindex', index)
  def command(*args, **kwargs):
    state['commands'].append(args)
    if args[:3] == ('nmcli', '-g', 'GENERAL.NM-MANAGED'):
      return state['names'][args[-1]]['managed']
    if args[:3] == ('nmcli', 'device', 'set'):
      entry = state['names'][args[3]]
      entry['managed'] = args[-1]
      if args[-1] == 'no' and state['remove_on_unmanage']:
        entry['addr'] = []
      return ''
    if args[:3] == ('ip', '-j', '-4'):
      if args[3] == 'addr':
        name = args[-1]
        entry = state['names'][name]
        return json.dumps([{'ifname': name, 'flags': ['UP'] if entry['up'] else [], 'addr_info': entry['addr']}])
      if args[3:5] == ('route', 'show'):
        return json.dumps(state['routes'])
      if args[3:5] == ('route', 'get'):
        return json.dumps(state['routes'] or [{'dev': 'wlan0', 'gateway': '192.168.45.1'}])
    if args[:3] == ('ip', 'link', 'set'):
      state['names'][args[4]]['up'] = args[-1] == 'up'
      return ''
    if args[:2] == ('ip', 'addr'):
      entry = state['names'][args[-1]]
      entry['addr'] = [{'local': ncm.LOCAL, 'prefixlen': 24}] if args[2] == 'add' else []
      return ''
    if args[:3] == ('ip', 'route', 'add'):
      state['routes'] = [{'dev': args[5], 'prefsrc': args[7], 'protocol': '186'}]
      return ''
    if args[:3] == ('ip', 'route', 'del'):
      state['routes'] = []
      return ''
    raise AssertionError(args)
  monkeypatch.setattr(ncm, 'run', command)
  state['add'] = add
  return state


def test_nm_address_loss_repair_and_only_owned_state_is_rolled_back(linux):
  config = ncm.configuration(IFACE, SHA)
  original = linux['names'][IFACE]
  original['up'] = False
  status = ncm.network_once(config)
  assert status['state'] == 'ready' and status['interface'] == IFACE
  assert original['managed'] == 'no' and original['up'] and original['addr']
  original['addr'] = []  # Reproduce NM/reconnect loss, without restarting host/HUD.
  ncm.network_once(config)
  assert original['addr'] and len(linux['routes']) == 1
  ncm.restore_network()
  assert original['managed'] == 'yes' and not original['up'] and original['addr'] == []
  assert linux['routes'] == []
  assert not any('systemctl' in c or 'default' in c or 'wlan0' in c for c in linux['commands'])


def test_preexisting_manual_address_survives_nm_release_and_rollback(linux):
  entry = linux['names'][IFACE]
  entry['addr'] = [{'local': ncm.LOCAL, 'prefixlen': 24}]
  linux['remove_on_unmanage'] = True
  ncm.network_once(ncm.configuration(IFACE, SHA))
  assert entry['addr']
  ncm.restore_network()
  assert entry['managed'] == 'yes' and entry['addr']


def test_detach_renamed_reenumeration_and_ambiguous_devices(linux):
  config = ncm.configuration(IFACE, SHA)
  ncm.network_once(config)
  (ncm.NET / IFACE / 'carrier').unlink()
  (ncm.NET / IFACE).rmdir()
  del linux['names'][IFACE]
  linux['routes'] = []  # Kernel drops routes when ifindex disappears.
  with pytest.raises(ValueError, match='Expected one'):
    ncm.network_once(config)
  linux['add']('enxRecreated', 8)
  assert ncm.network_once(config)['interface'] == 'enxRecreated'
  linux['add']('enxAmbiguous', 9)
  with pytest.raises(ValueError, match='Expected one'):
    ncm.network_once(config)
  ncm.restore_network()
  assert linux['names']['enxRecreated']['managed'] == 'yes'


@pytest.mark.parametrize('kind', ['wifi', 'hud', 'wrong-comma', 'route', 'prefix', 'nm-unknown'])
def test_wrong_device_or_conflicting_configuration_is_never_mutated(linux, kind):
  entry = linux['names'][IFACE]
  if kind == 'wifi':
    entry['driver'] = 'wifi'
  elif kind in ('hud', 'wrong-comma'):
    (entry['usb'] / 'idVendor').write_text('1cbe' if kind == 'hud' else 'ffff')
  elif kind == 'route':
    linux['routes'] = [{'dev': 'wlan0', 'gateway': '192.168.45.1'}]
  elif kind == 'prefix':
    entry['addr'] = [{'local': ncm.LOCAL, 'prefixlen': 16}]
  else:
    entry['managed'] = 'unknown'
  with pytest.raises(ValueError):
    ncm.network_once(ncm.configuration(IFACE, SHA))
  assert not any(c[:2] in (('ip', 'link'), ('ip', 'addr')) or c[:3] == ('nmcli', 'device', 'set') for c in linux['commands'])


def test_setup_failure_retains_intent_for_restore(linux, monkeypatch):
  original = ncm.run
  def fail(*args):
    if args[:3] == ('ip', 'addr', 'add'):
      raise subprocess.CalledProcessError(2, args)
    return original(*args)
  monkeypatch.setattr(ncm, 'run', fail)
  with pytest.raises(subprocess.CalledProcessError):
    ncm.network_once(ncm.configuration(IFACE, SHA))
  assert (ncm.RUN / 'nexo-ncm-leases.json').exists()
  monkeypatch.setattr(ncm, 'run', original)
  ncm.restore_network()
  assert linux['names'][IFACE]['managed'] == 'yes'


def test_ram_units_replay_saved_config_without_touching_hud_or_root(linux, tmp_path, monkeypatch):
  root = tmp_path / 'DATA/runtime'
  path = ncm.config_path(root)
  path.parent.mkdir(parents=True)
  config = ncm.configuration(IFACE, SHA)
  path.write_text(json.dumps(config))
  calls = []
  monkeypatch.setattr(ncm, 'run', lambda *args, **kw: calls.append(args) or '')
  ncm.activate(root)
  server = ncm.RUN / 'systemd/system/carrot-jetlink.service.d/nexo-ncm.conf'
  text = server.read_text()
  assert '--transport tcp --host 192.168.100.250 --port 5599' in text
  assert 'AmbientCapabilities=CAP_NET_RAW' in text and SHA in text and IFACE in text
  assert 'carrot-jetlink-hud.service' not in ''.join(map(str, calls))
  assert not (tmp_path / 'etc').exists()
  assert calls == [('systemctl', 'daemon-reload'), ('systemctl', 'start', ncm.UNIT)]
  ncm.deactivate(root)
  assert not server.exists() and path.exists()  # Caller owns the DATA transaction.


def test_conflicting_override_refused_and_ready_lease_must_be_fresh(linux, tmp_path, monkeypatch):
  root = tmp_path / 'runtime'
  config = ncm.configuration(IFACE, SHA)
  path = ncm.config_path(root)
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps(config))
  files = ncm.unit_files(root, config)
  first = next(iter(files))
  first.parent.mkdir(parents=True)
  first.write_text('# other owner\n')
  monkeypatch.setattr(ncm, 'run', lambda *a, **kw: '')
  with pytest.raises(ValueError, match='Unknown runtime'):
    ncm.activate(root)
  first.unlink()
  now = [100.]
  monkeypatch.setattr(ncm.time, 'monotonic', lambda: now[0])
  monkeypatch.setattr(ncm.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
  (ncm.RUN / 'nexo-ncm-status.json').write_text(json.dumps({'state': 'ready', 'configuration': ncm.signature(config), 'updated': 0.}))
  with pytest.raises(RuntimeError, match='did not become ready'):
    ncm.activate(root, wait=True)


@pytest.mark.parametrize('output', [{}, {'outputs': np.zeros((2576,), np.float32)},
                                  {'outputs': np.zeros((1, 2576), np.float16)}, {'outputs': np.full((1, 2576), np.nan, np.float32)}])
def test_wrong_engine_outputs_rejected(output):
  with pytest.raises(ValueError, match='contract'):
    validate_outputs(output, {'outputs': [1, 2576]})


def test_exact_native_engine_output_contract():
  validate_outputs({'outputs': np.zeros((1, 2576), np.float32)}, {'outputs': [1, 2576]})


@pytest.mark.parametrize('suffix', ['', '%enx36770f430ea2', '%7'])
def test_listener_must_belong_to_expected_service(linux, monkeypatch, suffix):
  def command(*args):
    if args[0] == 'systemctl':
      return '123'
    return f'LISTEN 0 1 192.168.100.250{suffix}:5599 0.0.0.0:* users:(("python",pid=123,fd=1))'
  monkeypatch.setattr(ncm, 'run', command)
  ncm.check_listener()
  monkeypatch.setattr(ncm, 'run', lambda *args: '123' if args[0] == 'systemctl' else 'LISTEN 0 1 0.0.0.0:5599 *:* pid=456,')
  with pytest.raises(RuntimeError, match='does not own'):
    ncm.check_listener(timeout=0)


def test_late_device_and_repeated_boot_config_restore_without_host_restart(linux, tmp_path, monkeypatch):
  root = tmp_path / 'DATA/runtime'
  config = ncm.configuration(IFACE, SHA)
  path = ncm.config_path(root)
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps(config))
  (ncm.NET / IFACE / 'carrier').unlink()
  (ncm.NET / IFACE).rmdir()
  del linux['names'][IFACE]
  states, turns = [], [0]
  original = ncm.write
  def observe(target, value, mode=0o644):
    if target.name == 'nexo-ncm-status.json':
      states.append(json.loads(value)['state'])
    original(target, value, mode)
  monkeypatch.setattr(ncm, 'write', observe)
  monkeypatch.setattr(ncm.signal, 'signal', lambda *a: None)
  def tick(delay):
    turns[0] += 1
    if turns[0] == 1:
      linux['add'](IFACE, 8)
    else:
      path.unlink()  # Explicit disable terminates the worker and restores NM.
  monkeypatch.setattr(ncm.time, 'sleep', tick)
  ncm.network_loop(root)
  assert states == ['error', 'ready']
  assert linux['names'][IFACE]['managed'] == 'yes'
  assert not any('systemctl' in c for c in linux['commands'])


def test_no_configuration_is_default_off(linux, tmp_path):
  ncm.activate(tmp_path / 'no-installed-opt-in')
  assert linux['commands'] == [] and list(ncm.RUN.iterdir()) == []
