"""DATA-only protected-image installation and boot recovery; never writes the base OS.

Copied to the persistent updater directory. Its small updater wrapper delegates
to the saved Carrot updater when Carrot is selected and keeps the Carrot channel
from replacing an explicitly installed NEXO runtime. An unfinished installation
restores the previous release and model before boot starts either USB owner.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path('/opt/carrot-jetlink')
UNITS = ('carrot-jetlink.service', 'carrot-jetlink-hud.service')
WRAPPER = "# NEXO protected updater bridge v1\nimport runpy\nrunpy.run_path('/opt/carrot-jetlink/updater/nexo_protected.py', run_name='__main__')\n"


def digest(path):
  result = hashlib.sha256()
  with path.open('rb') as stream:
    for block in iter(lambda: stream.read(4 << 20), b''):
      result.update(block)
  return result.hexdigest()


def durable(path, value, mode=0o644):
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = None
  try:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.nexo-', delete=False) as stream:
      temporary = Path(stream.name)
      stream.write(value)
      stream.flush()
      os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
  finally:
    if temporary is not None:
      temporary.unlink(missing_ok=True)
  sync_dir(path.parent)


def sync_dir(path):
  if os.name == 'posix':
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(descriptor)
    finally:
      os.close(descriptor)


def store(path, value):
  durable(path, json.dumps(value, allow_nan=False).encode())


def run(*args, timeout=30):
  return subprocess.check_output(args, text=True, timeout=timeout).strip()


def storage_guard():
  if os.geteuid() != 0 or os.uname().machine != 'aarch64':
    raise RuntimeError('Root on the supported Jetson is required')
  expected = {'format': 1, 'root': '/dev/mmcblk0p1', 'data': '/dev/mmcblk0p17', 'setup': '/dev/mmcblk0p16'}
  if json.loads(Path('/etc/carrot-jetlink-protected.json').read_text()) != expected:
    raise ValueError('Unsupported protected SD layout')
  state = json.loads(Path('/run/carrot-storage.json').read_text())
  if state.get('state') != 'protected' or state.get('system_read_only') is not True:
    raise ValueError('Protected storage is not ready')
  if ROOT.is_symlink() or not os.path.ismount(ROOT):
    raise ValueError('Persistent runtime bind mount is required')
  mount = json.loads(run('findmnt', '-J', '-T', str(ROOT), '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'))['filesystems'][0]
  if (mount['target'] != str(ROOT) or mount['source'] != '/dev/mmcblk0p17[/runtime]'
      or mount['fstype'] != 'ext4' or 'rw' not in mount['options'].split(',')):
    raise ValueError('Runtime is not the expected writable DATA bind mount')
  if json.loads((ROOT / 'protected-runtime.json').read_text()) != {'format': 1}:
    raise ValueError('Persistent runtime marker is missing')
  for path in ('releases', 'cache', 'cache/models', 'cache/engines', 'updater', 'updates'):
    candidate = ROOT / path
    if candidate.is_symlink():
      raise ValueError('Runtime directories cannot be symlinks')
  if not run('dpkg-query', '-W', '-f=${Version}', 'nvidia-l4t-core').startswith('36.4.7-'):
    raise ValueError('This installer requires L4T 36.4.7')


def selected_release():
  link = ROOT / 'current'
  if not link.is_symlink():
    raise ValueError('Existing current release must be a symlink')
  release = link.resolve(strict=True)
  return checked_release(release.name)


def checked_release(name):
  if not isinstance(name, str) or re.fullmatch(r'(?:nexo-)?[0-9a-f]{40}', name) is None:
    raise ValueError('Invalid saved release name')
  release = ROOT / 'releases' / name
  if release.is_symlink() or not release.is_dir() or release.resolve().parent != (ROOT / 'releases').resolve():
    raise ValueError('Release is outside the persistent release directory')
  return release


def switch_release(name):
  checked_release(name)
  temporary = ROOT / 'current.nexo-next'
  temporary.unlink(missing_ok=True)
  temporary.symlink_to('releases/' + name)
  os.replace(temporary, ROOT / 'current')
  sync_dir(ROOT)


def extract(bundle, destination):
  """Validate the entire archive before extracting any file, including on Python 3.10."""
  with tarfile.open(bundle, 'r:gz') as source:
    members = source.getmembers()
    seen, size = set(), 0
    for item in members:
      path = PurePosixPath(item.name)
      if (not item.name or path.is_absolute() or '..' in path.parts or '\\' in item.name
          or ':' in item.name or not (item.isdir() or item.isfile()) or item.name in seen):
        raise ValueError('Unsafe or duplicate bundle member')
      seen.add(item.name)
      size += item.size
      if size > 512 << 20 or len(seen) > 16000:
        raise ValueError('Bundle exceeds extraction budget')
    for item in members:
      path = destination / item.name
      if item.isdir():
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
      else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with source.extractfile(item) as data, path.open('xb') as output:
          shutil.copyfileobj(data, output)
        path.chmod(0o644)


def validate_marker(marker):
  if (marker.get('format') != 1 or re.fullmatch('[0-9a-f]{40}', str(marker.get('source_commit', ''))) is None
      or marker.get('carrot_commit') != 'd0dc96fa32410403dfb2fefff3e2909c11ec109b'
      or marker.get('runtime') != {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'}):
    raise ValueError('Unsupported NEXO runtime identity')
  model = marker.get('model', {})
  if (re.fullmatch('[0-9a-f]{64}', str(model.get('sha256', ''))) is None
      or type(model.get('nbytes')) is not int or not 0 < model['nbytes'] < 256 << 20 or model.get('frame_skip') != 4):
    raise ValueError('Invalid NEXO model identity')


def recover():
  transaction = ROOT / 'updates/nexo-install-transaction.json'
  if not transaction.exists():
    return False
  saved = json.loads(transaction.read_text())
  checked_release(saved['release'])
  raw = base64.b64decode(saved['last_loaded'], validate=True)
  if len(raw) > 65536:
    raise ValueError('Invalid recovery model metadata')
  model = json.loads(raw)
  if re.fullmatch('[0-9a-f]{64}', str(model.get('sha256', ''))) is None:
    raise ValueError('Invalid recovery model identity')
  switch_release(saved['release'])
  durable(ROOT / 'cache/last-loaded.json', raw)
  # Existing cache is managed by the service account, including remembered model.
  import pwd
  owner = pwd.getpwnam('jetlink')
  os.chown(ROOT / 'cache/last-loaded.json', owner.pw_uid, owner.pw_gid)
  store(ROOT / 'updates/nexo-install-status.json', {'state': 'rolled_back', 'release': saved['release']})
  os.sync()
  transaction.unlink()
  sync_dir(transaction.parent)
  return True


def install_bridge():
  updater = ROOT / 'updater'
  original, target = updater / 'update_host.carrot-original.py', updater / 'update_host.py'
  identity = updater / 'nexo-carrot-updater.json'
  if target.is_symlink() or original.is_symlink() or identity.is_symlink():
    raise ValueError('Updater files cannot be symlinks')
  if target.read_text() != WRAPPER:
    if original.exists() or identity.exists():
      raise ValueError('Unknown existing updater backup; refusing to overwrite it')
    raw = target.read_bytes()
    durable(original, raw)
    store(identity, {'sha256': hashlib.sha256(raw).hexdigest()})
  elif digest(original) != json.loads(identity.read_text())['sha256']:
    raise ValueError('Carrot updater backup checksum differs')
  # The guard lives independently of current; it also runs before USB services
  # when an interrupted install has to be recovered on the next boot.
  durable(updater / 'nexo_protected.py', Path(__file__).read_bytes())
  durable(target, WRAPPER.encode())


def probe(release):
  command = ['runuser', '-u', 'jetlink', '--', str(ROOT / 'venv/bin/python'),
             str(release / 'openpilot/tools/jetson/protected_probe.py'), str(ROOT / 'cache')]
  process = subprocess.Popen(command, start_new_session=True)
  try:
    if process.wait(timeout=960):
      raise RuntimeError('NEXO TensorRT/HUD candidate probe failed')
  except BaseException:
    try:
      os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    process.wait()
    raise


def active():
  return all(subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0 for unit in UNITS)


def install(bundle, sha256):
  storage_guard()
  if re.fullmatch('[0-9a-f]{64}', sha256) is None or bundle.is_symlink() or digest(bundle) != sha256:
    raise ValueError('Bundle checksum differs')
  updates = ROOT / 'updates'
  updates.mkdir(exist_ok=True)
  for name in ('pending.json', 'transaction.json'):
    if (updates / name).exists():
      raise RuntimeError('A Carrot update is pending; refusing a competing installation')
  if (updates / 'nexo-install-transaction.json').exists():
    raise RuntimeError('An interrupted NEXO install needs boot recovery first')
  previous = selected_release()
  if not active():
    raise RuntimeError('Existing server and HUD must be active before installation')
  for unit in UNITS:
    command = run('systemctl', 'show', unit, '-p', 'ExecStart', '--value')
    expected = '/opt/carrot-jetlink/current/tools/jetlink/' + ('hud.py' if 'hud' in unit else 'server.py')
    if expected not in command or '/opt/carrot-jetlink/venv/bin/python' not in command:
      raise ValueError('Existing service does not use the expected protected entry point')
  updater_command = run('systemctl', 'show', 'carrot-jetlink-update-apply.service', '-p', 'ExecStart', '--value')
  if '/opt/carrot-jetlink/updater/update_host.py activate' not in updater_command:
    raise ValueError('Boot updater does not use the supported recovery entry point')
  if shutil.disk_usage(ROOT).free < 4 << 30:
    raise RuntimeError('At least 4 GiB free DATA space is required')
  versions = json.loads(run(str(ROOT / 'venv/bin/python'), '-c',
                           'import sys,tensorrt,json;print(json.dumps([list(sys.version_info[:2]),tensorrt.__version__]))'))
  if versions != [[3, 10], '10.3.0']:
    raise ValueError('Installed Python/TensorRT differs from the tested host ABI')
  with tempfile.TemporaryDirectory(dir=ROOT / 'releases', prefix='.nexo-stage-') as temporary:
    candidate = Path(temporary)
    extract(bundle, candidate)
    marker = json.loads((candidate / 'NEXO_RUNTIME.json').read_text())
    validate_marker(marker)
    if (candidate / 'SOURCE_COMMIT').read_text().strip() != marker['source_commit']:
      raise ValueError('Bundle commit identities differ')
    if digest(candidate / 'openpilot/tools/jetson/protected_install.py') != digest(Path(__file__)):
      raise ValueError('Installer differs from the committed bundle')
    model = candidate / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
    if model.stat().st_size != marker['model']['nbytes'] or digest(model) != marker['model']['sha256']:
      raise ValueError('Bundled NEXO model checksum differs')
    name = 'nexo-' + marker['source_commit']
    release = ROOT / 'releases' / name
    if release.exists():
      if release.is_symlink() or (release / '.nexo-bundle-sha256').read_text() != sha256:
        raise ValueError('Existing release differs from the uploaded bundle')
    else:
      (candidate / '.nexo-bundle-sha256').write_text(sha256)
      candidate.chmod(0o755)
      os.rename(candidate, release)
      sync_dir(release.parent)
  if previous == release:
    print('NEXO_ALREADY_INSTALLED', marker['source_commit'], flush=True)
    return
  model = release / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
  remembered = ROOT / 'cache/last-loaded.json'
  raw = remembered.read_bytes()
  if len(raw) > 65536 or re.fullmatch('[0-9a-f]{64}', str(json.loads(raw).get('sha256', ''))) is None:
    raise ValueError('Previous model recovery metadata is invalid')
  saved = {'release': previous.name, 'last_loaded': base64.b64encode(raw).decode()}
  # All source staging precedes stopping services. Persist recovery before any
  # GPU probe changes last-loaded.json or current. The bridge never writes /etc.
  store(updates / 'nexo-install-transaction.json', saved)
  try:
    install_bridge()
    os.sync()
    run('systemctl', 'stop', *UNITS, timeout=60)
    if any(subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0 for unit in UNITS):
      raise RuntimeError('Existing USB owners did not stop')
    import pwd
    owner = pwd.getpwnam('jetlink')
    destination = ROOT / 'cache/models' / (marker['model']['sha256'][:16] + '.onnx')
    if destination.is_symlink():
      raise ValueError('Model cache cannot be a symlink')
    if destination.exists():
      if digest(destination) != marker['model']['sha256']:
        raise ValueError('A different model occupies the expected cache key')
    else:
      durable(destination, model.read_bytes())
    os.chown(destination, owner.pw_uid, owner.pw_gid)
    store(updates / 'nexo-install-status.json', {'state': 'testing', 'source_commit': marker['source_commit']})
    probe(release)
    loaded = json.loads(remembered.read_text())
    if loaded != {'sha256': marker['model']['sha256'], 'frame_skip': 4, 'backend': 'trt'}:
      raise ValueError('NEXO model was not remembered for the next boot')
    switch_release(name)
    run('systemctl', 'start', *UNITS)
    time.sleep(10)
    if not active():
      raise RuntimeError('NEXO server/HUD service failed to remain active')
    store(updates / 'nexo-install-previous.json', saved)
    store(updates / 'nexo-install-status.json', {'state': 'installed', 'source_commit': marker['source_commit'],
                                               'model_sha256': marker['model']['sha256'], 'synthetic_inference': True,
                                               'vehicle_inference_verified': False, 'physical_hud_verified': False})
    os.sync()
    (updates / 'nexo-install-transaction.json').unlink()
    sync_dir(updates)
    print('NEXO_INSTALL_OK', marker['source_commit'], flush=True)
  except BaseException:
    run('systemctl', 'stop', *UNITS, timeout=60)
    recover()
    run('systemctl', 'start', *UNITS)
    raise


def updater(action):
  storage_guard()
  if (ROOT / 'updates/nexo-install-transaction.json').exists():
    if action != 'activate' or any(subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0 for unit in UNITS):
      raise RuntimeError('Interrupted installation requires boot recovery before USB services start')
    recover()
  if (selected_release() / 'NEXO_RUNTIME.json').is_file():
    validate_marker(json.loads((selected_release() / 'NEXO_RUNTIME.json').read_text()))
    print('NEXO selected; keeping its pinned model and runtime instead of the Carrot update channel', flush=True)
    return
  original = ROOT / 'updater/update_host.carrot-original.py'
  identity = json.loads((ROOT / 'updater/nexo-carrot-updater.json').read_text())
  if original.is_symlink() or digest(original) != identity['sha256']:
    raise ValueError('Saved Carrot updater checksum differs')
  # Delegate outside our lock: the original updater uses the same lock itself.
  os.execv('/usr/bin/python3', ['/usr/bin/python3', str(original), *sys.argv[1:]])


def main():
  import fcntl
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=('install', 'activate', 'automatic', 'stage'))
  parser.add_argument('--bundle', type=Path)
  parser.add_argument('--sha256')
  args, _ = parser.parse_known_args()
  def interrupted(signum, frame):
    raise InterruptedError('Installation interrupted')
  signal.signal(signal.SIGTERM, interrupted)
  signal.signal(signal.SIGINT, interrupted)
  # This descriptor is non-inheritable; exec releases it before the Carrot
  # updater acquires its own lock. Both timer and manual installer share it.
  with Path('/run/carrot-jetlink-update.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.action == 'install':
      if args.bundle is None or args.sha256 is None:
        parser.error('install requires --bundle and --sha256')
      try:
        install(args.bundle, args.sha256)
      except Exception as error:
        # Keep the rollback state visible instead of leaving a queued/testing
        # record after a failed worker. Recheck DATA before diagnostic writes.
        storage_guard()
        path = ROOT / 'updates/nexo-install-status.json'
        status = json.loads(path.read_text()) if path.exists() else {'state': 'failed'}
        if status.get('state') != 'rolled_back':
          status['state'] = 'failed'
        status['error'] = f'{type(error).__name__}: {error}'[:320]
        store(path, status)
        raise
    else:
      updater(args.action)


if __name__ == '__main__':
  main()
