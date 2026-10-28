"""Explicit DATA configuration, volatile units and USB-only address recovery.

No APP/SETUP writes, NM profiles, default routes, gadget changes or model enable.
The protected updater replays DATA configuration before the existing services.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time

ROOT = Path('/opt/carrot-jetlink')
RUN = Path('/run')
NET = Path('/sys/class/net')
TAG = '# NEXO protected NCM v1\n'
UNIT = 'nexo-jetson-ncm.service'
BOOT_UNIT = 'carrot-jetlink-boot-display.service'
LOCAL, PEER = '192.168.100.250', '192.168.100.127'


def configuration(interface, model_sha256, display_only=False):
  value = {'format': 1, 'transport': 'ncm-tcp', 'interface': interface, 'model_sha256': model_sha256}
  if display_only:
    value['display_only'] = True
  validate(value)
  return value


def validate(value):
  if (not isinstance(value, dict) or set(value) - {'format', 'transport', 'interface', 'model_sha256', 'display_only'}
      or not {'format', 'transport', 'interface', 'model_sha256'} <= set(value)
      or ('display_only' in value and value['display_only'] is not True)
      or value['format'] != 1 or value['transport'] != 'ncm-tcp'
      or re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', str(value['interface'])) is None
      or value['interface'] in ('.', '..') or re.fullmatch('[0-9a-f]{64}', str(value['model_sha256'])) is None):
    raise ValueError('Invalid protected NCM configuration')
  return value


def config_path(root=ROOT):
  return root / 'updates/nexo-ncm.json'


def load(root=ROOT):
  path = config_path(root)
  if not path.exists():
    return None
  if path.is_symlink() or path.stat().st_size > 4096:
    raise ValueError('Unsafe protected NCM configuration')
  return validate(json.loads(path.read_bytes()))


def run(*args, timeout=5):
  return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout,
                                 env={**os.environ, 'LC_ALL': 'C'}).strip()


def write(path, value, mode=0o644):
  """Atomic RAM files only; persistent DATA writes use protected_install.store."""
  if not path.resolve().is_relative_to(RUN.resolve()) or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(RUN)):
    raise ValueError('NCM runtime writes must stay inside /run without symlink parents')
  path.parent.mkdir(parents=True, exist_ok=True)
  if path.is_symlink():
    raise ValueError(f'Refusing symlink {path}')
  with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
    temporary = Path(stream.name)
    stream.write(value.encode())
  try:
    temporary.chmod(mode)
    os.replace(temporary, path)
  finally:
    temporary.unlink(missing_ok=True)


def read_json(path, default):
  try:
    if path.is_symlink() or path.stat().st_size > 65536:
      raise ValueError('Unsafe NCM state file')
    return json.loads(path.read_bytes())
  except FileNotFoundError:
    return default


def usb_device(interface):
  net = NET / interface
  if (net / 'device/driver').resolve().name != 'cdc_ncm':
    raise ValueError(f'{interface} is not cdc_ncm')
  for parent in (net / 'device').resolve().parents:
    if (parent / 'idVendor').is_file() and (parent / 'idProduct').is_file():
      if (parent / 'idVendor').read_text().strip().lower() == '04d8' and (parent / 'idProduct').read_text().strip().lower() == '1234':
        return str(parent)
      break
  raise ValueError(f'{interface} is not the verified 04d8:1234 comma USB device')


def discover(preferred):
  if (NET / preferred).exists():
    usb_device(preferred)  # A reused name must not admit Ethernet/Wi-Fi/TURZX.
    return preferred
  candidates = []
  for path in NET.glob('*'):
    try:
      usb_device(path.name)
      candidates.append(path.name)
    except (OSError, ValueError):
      continue
  if len(candidates) != 1:
    raise ValueError(f'Expected one comma cdc_ncm device; found {candidates}')
  return candidates[0]


def ip_json(*args):
  value = json.loads(run('ip', '-j', '-4', *args))
  if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
    raise ValueError('Invalid ip JSON')
  return value


def signature(value):
  return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def network_once(value):
  validate(value)
  interface = discover(value['interface'])
  index, device = socket.if_nametoindex(interface), usb_device(interface)
  links = json.loads(run('ip', '-j', 'link', 'show', 'dev', interface))
  if (not isinstance(links, list) or len(links) != 1 or links[0].get('ifname') != interface
      or links[0].get('ifindex') != index or not isinstance(links[0].get('flags'), list)):
    raise ValueError('NCM interface disappeared during link inspection')
  rows = ip_json('addr', 'show', 'dev', interface)
  if rows and (len(rows) != 1 or rows[0].get('ifname') != interface):
    raise ValueError('NCM interface disappeared during address inspection')
  addresses = rows[0].get('addr_info', []) if rows else []
  if not isinstance(addresses, list) or any(not isinstance(row, dict) for row in addresses):
    raise ValueError('Invalid NCM address information')
  if any(row.get('local') == LOCAL and row.get('prefixlen') != 24 for row in addresses):
    raise ValueError('Existing NCM local address has an unexpected prefix')
  managed = run('nmcli', '-g', 'GENERAL.NM-MANAGED', 'device', 'show', interface)
  if managed not in ('yes', 'no'):
    raise ValueError('Cannot establish NetworkManager ownership')
  routes = ip_json('route', 'show', 'exact', PEER + '/32')
  if routes and (len(routes) != 1 or routes[0].get('dev') != interface or routes[0].get('gateway')
                 or routes[0].get('prefsrc', LOCAL) != LOCAL):
    raise ValueError('A conflicting peer host route exists; refusing to overwrite it')
  leases_path = RUN / 'nexo-ncm-leases.json'
  leases = read_json(leases_path, {})
  for old_key, old in list(leases.items()):
    try:
      present = socket.if_nametoindex(old['interface']) == old['index'] and usb_device(old['interface']) == old['device']
    except (OSError, ValueError):
      present = False
    if not present:
      del leases[old_key]  # The removed kernel device cannot retain an address/route.
  key = f'{interface}:{index}'
  lease = leases.setdefault(key, {'interface': interface, 'index': index, 'device': device,
                                 'managed': managed, 'up': 'UP' in links[0]['flags'],
                                 'address_added': not any(a.get('local') == LOCAL and a.get('prefixlen') == 24 for a in addresses),
                                 'route_added': not routes, 'original_host_route': bool(routes)})
  if lease['device'] != device:
    raise ValueError('NCM ifindex was reused by a different USB device')
  # Record intent before mutation, including a failure halfway through setup.
  write(leases_path, json.dumps(leases), 0o600)
  if usb_device(interface) != device or socket.if_nametoindex(interface) != index:
    raise ValueError('NCM interface changed before setup')
  if managed == 'yes':
    run('nmcli', 'device', 'set', interface, 'managed', 'no')
  def same_device():
    if usb_device(interface) != device or socket.if_nametoindex(interface) != index:
      raise ValueError('NCM interface changed during setup')
  same_device()
  if 'UP' not in links[0]['flags']:
    run('ip', 'link', 'set', 'dev', interface, 'up')
  # NM may remove its old address while relinquishing the device. Read again.
  current = ip_json('addr', 'show', 'dev', interface)
  present = any(a.get('local') == LOCAL and a.get('prefixlen') == 24 for row in current for a in row.get('addr_info', []))
  same_device()
  if not present:
    run('ip', 'addr', 'add', LOCAL + '/24', 'dev', interface, 'noprefixroute')
  if not ip_json('route', 'show', 'exact', PEER + '/32'):
    same_device()
    if lease['original_host_route']:
      raise ValueError('The original peer host route disappeared; refusing to replace its ownership')
    run('ip', 'route', 'add', PEER + '/32', 'dev', interface, 'src', LOCAL, 'proto', '186')
  if socket.if_nametoindex(interface) != index or usb_device(interface) != device:
    raise ValueError('NCM interface changed after setup')
  route = ip_json('route', 'get', PEER, 'from', LOCAL)
  if len(route) != 1 or route[0].get('dev') != interface or route[0].get('gateway'):
    raise ValueError('NCM peer does not have a direct USB route')
  carrier = (NET / interface / 'carrier').read_text().strip()
  return {'state': 'ready' if carrier == '1' else 'waiting_usb', 'interface': interface,
          'updated': time.monotonic(), 'configuration': signature(value), 'error': ''}


def restore_network():
  path = RUN / 'nexo-ncm-leases.json'
  leases = read_json(path, {})
  for key, lease in list(leases.items()):
    interface = lease['interface']
    try:
      same = (socket.if_nametoindex(interface) == lease['index'] and usb_device(interface) == lease['device'])
    except (OSError, ValueError):
      same = False
    if same:
      routes = ip_json('route', 'show', 'exact', PEER + '/32')
      if lease['route_added'] and any(row.get('dev') == interface and str(row.get('protocol')) == '186' for row in routes):
        run('ip', 'route', 'del', PEER + '/32', 'dev', interface, 'proto', '186')
      if lease['address_added']:
        addresses = ip_json('addr', 'show', 'dev', interface)
        if any(a.get('local') == LOCAL and a.get('prefixlen') == 24 for row in addresses for a in row.get('addr_info', [])):
          run('ip', 'addr', 'del', LOCAL + '/24', 'dev', interface)
      if not lease['up']:
        run('ip', 'link', 'set', 'dev', interface, 'down')
      run('nmcli', 'device', 'set', interface, 'managed', lease['managed'])
    del leases[key]
    write(path, json.dumps(leases), 0o600)
  path.unlink(missing_ok=True)


def unit_files(root, value):
  base = '/opt/carrot-jetlink'
  command = (f'{base}/venv/bin/python {base}/current/tools/jetlink/server.py --backend trt --transport tcp '
             + f'--host {LOCAL} --port 5599 --cache {base}/cache --control-socket {base}/control.sock')
  server = f'''{TAG}[Unit]
After=nexo-jetson-ncm.service
[Service]
ExecStart=
ExecStart={command}
Environment=NEXO_JETLINK_TRANSPORT=ncm-tcp
Environment=NEXO_JETLINK_USB_IFACE={value['interface']}
Environment=NEXO_JETLINK_MODEL_SHA256={value['model_sha256']}
Environment=NEXO_JETLINK_DISPLAY_ONLY={int(value.get('display_only', False))}
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
NoNewPrivileges=yes
'''
  worker = f'''{TAG}[Unit]
After=NetworkManager.service carrot-protected-storage.service
Before=carrot-jetlink.service
[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 {base}/updater/nexo_ncm.py network
Restart=on-failure
RestartSec=2
'''
  return {RUN / 'systemd/system/carrot-jetlink.service.d/nexo-ncm.conf': server,
          RUN / ('systemd/system/' + UNIT): worker}


def owned_file(path):
  if path.is_symlink() or (path.exists() and not path.read_text().startswith(TAG)):
    raise ValueError(f'Unknown runtime override: {path}')


def panel_files(root):
  paths = {RUN / f'systemd/system/{unit}.d/nexo-panel.conf': ''
           for unit in (BOOT_UNIT, 'carrot-jetlink-hud.service')}
  # Rollback to an older release restores APP's original boot screen.
  if (root / 'current/openpilot/tools/jetson/boot_display.py').is_file():
    shared = (TAG + f'# release: {(root / "current").resolve().name}\n[Service]\nUser=jetlink\nRuntimeDirectory=carrot-jetlink-display\n'
              'RuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n')
    paths[RUN / f'systemd/system/{BOOT_UNIT}.d/nexo-panel.conf'] = shared + (
      'ExecStart=\nExecStart=/opt/carrot-jetlink/venv/bin/python '
      '/opt/carrot-jetlink/current/tools/jetlink/boot_display.py\n')
    paths[RUN / 'systemd/system/carrot-jetlink-hud.service.d/nexo-panel.conf'] = shared
  return paths


def configure_panel(root, enabled=True):
  files = panel_files(root)
  for path in files:
    owned_file(path)
  changed = any((text if enabled else '') != (path.read_text() if path.exists() else '')
                for path, text in files.items())
  if not changed:
    return False
  # Stop the immutable old process before admitting DATA's new lock protocol.
  run('systemctl', 'stop', BOOT_UNIT, timeout=30)
  for path, text in files.items():
    if enabled and text:
      write(path, text)
    else:
      path.unlink(missing_ok=True)
  return True


def check_runtime_files(root, value):
  for path in (*unit_files(root, value), *panel_files(root)):
    owned_file(path)


def activate(root=ROOT, wait=False):
  value = load(root)
  if value is None:
    return
  check_runtime_files(root, value)
  panel_changed = configure_panel(root)
  for path, text in unit_files(root, value).items():
    write(path, text)
  run('systemctl', 'daemon-reload')
  run('systemctl', 'start', UNIT, timeout=30)
  if panel_changed:
    run('systemctl', 'start', BOOT_UNIT, timeout=30)
  if wait:
    started = time.monotonic()
    while time.monotonic() - started < 15:
      status = read_json(RUN / 'nexo-ncm-status.json', {})
      if (status.get('state') == 'ready' and status.get('configuration') == signature(value)
          and status.get('updated', 0) >= started and 0 <= time.monotonic() - status.get('updated', 0) < 3):
        return
      time.sleep(.25)
    raise RuntimeError('Protected NCM network did not become ready; inspect /run/nexo-ncm-status.json')


def deactivate(root=ROOT):
  panel_changed = configure_panel(root, enabled=False)
  files = unit_files(root, configuration('unused', '0' * 64))
  for path in files:
    owned_file(path)
  if (RUN / ('systemd/system/' + UNIT)).exists():
    run('systemctl', 'stop', UNIT, timeout=30)
  restore_network()
  for path in files:
    path.unlink(missing_ok=True)
  (RUN / 'nexo-ncm-status.json').unlink(missing_ok=True)
  run('systemctl', 'daemon-reload')
  if panel_changed:
    run('systemctl', 'start', BOOT_UNIT, timeout=30)


def check_listener(timeout=30):
  deadline = time.monotonic() + timeout
  endpoint = re.compile(re.escape(LOCAL) + r'(?:%[A-Za-z0-9_.:-]+)?:5599\Z')
  while True:
    try:
      pid = run('systemctl', 'show', 'carrot-jetlink.service', '-p', 'MainPID', '--value')
      listeners = run('ss', '-H', '-ltnp', 'sport', '=', ':5599')
      if pid.isdigit() and int(pid) > 0 and any(
          len(row := line.split()) >= 4 and endpoint.fullmatch(row[3]) and f'pid={pid},' in line for line in listeners.splitlines()):
        return
    except (OSError, subprocess.SubprocessError):
      pass
    if time.monotonic() >= deadline:
      raise RuntimeError('The NEXO service does not own the dedicated USB TCP listener')
    time.sleep(.5)


def network_loop(root=ROOT):
  running = [True]
  def stop(*args):
    running[0] = False
  signal.signal(signal.SIGTERM, stop)
  signal.signal(signal.SIGINT, stop)
  try:
    while running[0]:
      try:
        value = load(root)
        if value is None:
          break
        status = network_once(value)
      except (OSError, ValueError, subprocess.SubprocessError) as error:
        status = {'state': 'error', 'updated': time.monotonic(), 'error': f'{type(error).__name__}: {error}'[:400]}
      write(RUN / 'nexo-ncm-status.json', json.dumps(status))
      time.sleep(2)
  finally:
    restore_network()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=('network',))
  parser.parse_args()
  spec = importlib.util.spec_from_file_location('nexo_storage_guard', ROOT / 'updater/nexo_protected.py')
  guard = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(guard)
  guard.storage_guard()
  import fcntl
  with (RUN / 'nexo-ncm-worker.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    network_loop()


if __name__ == '__main__':
  main()
