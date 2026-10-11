"""Template bound to a specific compact bundle by the offline publisher."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

NEXO_SHA = 'NEXO_SHA_PLACEHOLDER'
NEXO_BYTES = 0  # NEXO_BYTES_PLACEHOLDER
NEXO_SOURCE = 'NEXO_SOURCE_PLACEHOLDER'


def nexo_offline_boot(system_root=Path('/')):
  if json.loads((system_root / 'run/carrot-storage.json').read_text()).get('state') != 'protected':
    return  # Preserve factory recovery when DATA did not boot.
  from protected_storage import setup_mount
  root = system_root / 'opt/carrot-jetlink'
  folder = root / 'offline-nexo' / NEXO_SHA
  if (root / 'offline-nexo').is_symlink() or folder.is_symlink():
    raise ValueError('Unexpected offline cache link')
  folder.mkdir(parents=True, exist_ok=True)
  archive = folder / 'runtime.tar.gz'

  def verified(path):
    if path.is_symlink():
      return False
    try:
      if path.stat().st_size != NEXO_BYTES:
        return False
      digest = hashlib.sha256()
      with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 << 20), b''):
          digest.update(block)
      return digest.hexdigest() == NEXO_SHA
    except OSError:
      return False

  if not verified(archive):
    with setup_mount() as identity:
      if identity is None:
        raise RuntimeError('Offline SETUP is unavailable')
      source = identity.parent / 'nexo-offline-runtime.tar.gz'
      if not verified(source):
        raise ValueError('Offline runtime checksum differs')
      temporary = folder / 'runtime.new'
      if temporary.is_symlink():
        raise ValueError('Unexpected offline cache link')
      with source.open('rb') as reader, temporary.open('wb') as writer:
        for block in iter(lambda: reader.read(4 << 20), b''):
          writer.write(block)
        writer.flush()
        import os
        os.fsync(writer.fileno())
      if not verified(temporary):
        raise ValueError('Offline runtime copy differs')
      temporary.replace(archive)
  # The entire SHA-verified bundle is the trust boundary. Execute only these
  # exact regular helper members; all other extraction uses protected_install.
  with tarfile.open(archive, 'r:gz') as package:
    helpers = {f'openpilot/tools/jetson/{name}': name for name in ('protected_install.py', 'offline_bootstrap.py')}
    members = [item for item in package if item.name in helpers]
    if len(members) != len(helpers) or {item.name for item in members} != set(helpers):
      raise ValueError('Offline helpers are missing or duplicated')
    for item in members:
      if not item.isfile() or not 0 < item.size < 128 << 10:
        raise ValueError('Unexpected offline helper member')
      target = folder / helpers[item.name]
      if target.is_symlink():
        raise ValueError('Unexpected offline helper link')
      data = package.extractfile(item).read()
      if not target.exists() or target.read_bytes() != data:
        target.write_bytes(data)
        target.chmod(0o644)
  # No waits inside image-setup: the new job must start AFTER that very service.
  # Factory inference/HUD start first, then the protected installer stops both
  # before its GPU probe and atomically selects the new runtime.
  subprocess.run(['systemd-run', '--no-block', '--unit=nexo-offline-install',
                  '--property=After=carrot-image-setup.service carrot-jetlink-update-apply.service carrot-jetlink.service carrot-jetlink-hud.service',
                  '--property=RuntimeMaxSec=1200', '--property=TimeoutStopSec=30', '--property=KillMode=control-group',
                  '/usr/bin/python3', str(folder / 'offline_bootstrap.py'), '--bundle', str(archive),
                  '--sha256', NEXO_SHA, '--source', NEXO_SOURCE], check=True, timeout=15)


# Publisher calls this after original main; errors must preserve factory boot.
