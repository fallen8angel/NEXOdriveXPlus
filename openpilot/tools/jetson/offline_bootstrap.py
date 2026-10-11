"""Run the existing protected installer after factory services start, without SSH.

Executed only from the bundle verified by the immutable APP bootstrap. Existing
identity, networking, model selection and rollback remain with the installer.
Terminal failures are not retried every boot. Physical HUD/vehicle inference
are never inferred from a completed installation.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time

ROOT = Path('/opt/carrot-jetlink')


def usb_network_detached(sysfs=Path('/sys/class/net'), usb=Path('/sys/bus/usb/devices')):
  """First offline activation requires Comma USB physically detached.

  Do not stop a server that could be serving a vehicle. The USB NIC check is
  independent of link-local address discovery/SSH, both unavailable here.
  """
  if not sysfs.is_dir() or not usb.is_dir():
    return False
  for device in usb.iterdir():
    vendor, product = device / 'idVendor', device / 'idProduct'
    if not vendor.exists() and not product.exists():
      continue  # Interface entries, rather than USB device entries.
    try:
      # This is the exact pinned Jetlink bulk VID/PID, not an IP address.
      if (int(vendor.read_text().strip(), 16), int(product.read_text().strip(), 16)) == (0x1209, 0x0001):
        return False
    except (OSError, ValueError):
      return False
  for interface in sysfs.iterdir():
    device = interface / 'device'
    # A DOWN network interface is still physically attached. Also cover old
    # composite network-only gadgets that don't use the pinned bulk VID/PID.
    if device.exists() and re.search(r'/usb[0-9]+(?:/|$)', str(device.resolve())):
      return False
  return True


def load_installer(folder):
  spec = importlib.util.spec_from_file_location('nexo_protected_installer', folder / 'protected_install.py')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def verify_helpers(bundle, folder, sha256):
  if bundle.is_symlink():
    raise ValueError('Offline bundle cannot be a link')
  digest = hashlib.sha256()
  with bundle.open('rb') as stream:
    for block in iter(lambda: stream.read(4 << 20), b''):
      digest.update(block)
  if digest.hexdigest() != sha256:
    raise ValueError('Offline bundle checksum differs')
  names = {f'openpilot/tools/jetson/{name}': name for name in ('protected_install.py', 'offline_bootstrap.py')}
  with tarfile.open(bundle) as package:
    members = [item for item in package if item.name in names]
    if len(members) != len(names) or {item.name for item in members} != set(names):
      raise ValueError('Missing/duplicate offline helpers')
    for item in members:
      target = folder / names[item.name]
      if (not item.isfile() or not 0 < item.size < 128 << 10 or target.is_symlink() or
          target.read_bytes() != package.extractfile(item).read()):
        raise ValueError('Cached offline helper differs')


def export_result(value):
  # This fixed OS helper resolves SETUP on the same protected SD, never a
  # removable disk selected by label. No identity/profile/journal is exported.
  sys.path.insert(0, '/usr/lib/carrot-jetlink-storage')
  from protected_storage import setup_mount
  with setup_mount(writable=True) as identity:
    if identity is None:
      raise RuntimeError('SETUP result partition is unavailable')
    from persistent_state import durable_write
    durable_write(identity.parent / 'NEXO-OFFLINE-RESULT.json', json.dumps(value).encode(), 0o600)


def worker(bundle, sha256, source):
  if not re.fullmatch('[0-9a-f]{64}', sha256) or not re.fullmatch('[0-9a-f]{40}', source):
    raise ValueError('Invalid offline bundle identity')
  expected = ROOT / 'offline-nexo' / sha256
  if bundle != expected / 'runtime.tar.gz' or expected.is_symlink() or not expected.is_dir() or expected.parent.is_symlink():
    raise ValueError('Unexpected offline bundle location')
  verify_helpers(bundle, expected, sha256)
  module = load_installer(expected)
  module.storage_guard()
  if module.digest(bundle) != sha256:
    raise ValueError('Offline bundle checksum differs')
  terminal = expected / 'result.json'
  if terminal.exists():
    value = json.loads(terminal.read_text())
    export_result(value)
    return value
  value = {'format': 1, 'state': 'failed', 'source_commit': source,
           'installation_started': False, 'vehicle_inference_verified': False, 'physical_hud_verified': False}
  # No permanently failed result: unplug Comma USB before the next boot to
  # permit an offline install. An attached vehicle keeps its factory services.
  for _ in range(3):
    if not usb_network_detached():
      value['state'] = 'deferred_usb_connected'
      export_result(value)
      return value
    time.sleep(1)
  module.store(expected / 'started.json', {'source_commit': source})
  try:
    # After= ordering is installed by the APP hook, and this bounded wait also
    # handles a slow factory service. No new USB owner is started alongside it.
    deadline = time.monotonic() + 60
    while not module.active() and time.monotonic() < deadline:
      time.sleep(1)
    if not module.active():
      raise RuntimeError('Factory server/HUD did not start')
    # Invoke the same CLI, lock, signal handlers, real TRT probe and recovery
    # transaction as SSH installation. A systemd job owns the process tree.
    value['installation_started'] = True
    result = subprocess.run(['/usr/bin/python3', str(expected / 'protected_install.py'), 'install',
                             '--bundle', str(bundle), '--sha256', sha256], check=False)
    status_file = ROOT / 'updates/nexo-install-status.json'
    status = json.loads(status_file.read_text()) if status_file.exists() else {}
    current = module.selected_release()
    if (result.returncode != 0 or current.name != 'nexo-' + source or not module.active() or
        status.get('state') != 'installed' or status.get('source_commit') != source):
      raise RuntimeError('Protected installer did not confirm activation')
    value.update(state='installed', synthetic_inference=status.get('synthetic_inference') is True)
  except Exception as error:
    # Never export raw exceptions, logs, SSIDs or key locations to SETUP.
    value['error_type'] = type(error).__name__
  module.store(terminal, value)
  export_result(value)
  return value


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--bundle', required=True, type=Path)
  parser.add_argument('--sha256', required=True)
  parser.add_argument('--source', required=True)
  args = parser.parse_args()
  value = worker(args.bundle, args.sha256, args.source)
  print('NEXO_OFFLINE_RESULT=' + json.dumps(value), flush=True)
  return 0 if value['state'] == 'installed' else 1


if __name__ == '__main__':
  raise SystemExit(main())
