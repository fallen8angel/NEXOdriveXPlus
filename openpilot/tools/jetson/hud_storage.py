"""Conservative HUD upload cleanup. Never traverses runtime, model or experiment trees."""
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import sys
import time

FILES = frozenset(('nexo-hud-repair.tar.gz', 'nexo-hud-repair.json', 'protected_install.py',
                   'protected_ncm.py', 'install_hud_update.py', 'SHA256SUMS', 'Connect-Jetson.ps1', 'INSTALL_KO.md'))
MIN_FREE = 4 << 30  # Keep the protected installer's requirement, plus the incoming upload.


def inventory(root, incoming, now=None):
  if not re.fullmatch('[0-9a-f]{40}', incoming):
    raise ValueError('Invalid incoming HUD commit')
  updates = root / 'updates'
  if root.is_symlink() or updates.is_symlink() or not updates.is_dir():
    raise ValueError('Unsafe DATA update directory')
  for name in ('pending.json', 'transaction.json', 'nexo-install-transaction.json'):
    if (updates / name).exists() or (updates / name).is_symlink():
      raise RuntimeError('Pending update or recovery; cleanup refused')
  current = (root / 'current').resolve(strict=True)
  if (not (root / 'current').is_symlink() or current.parent != (root / 'releases').resolve()
      or not re.fullmatch('(nexo-)?[0-9a-f]{40}', current.name)):
    raise ValueError('Cannot establish current release')
  protected = {incoming[:12], current.name.removeprefix('nexo-')[:12]}
  for name in ('previous.json', 'status.json', 'nexo-install-previous.json', 'nexo-install-status.json'):
    path = updates / name
    if path.is_symlink():
      raise ValueError('Unsafe recovery record')
    if not path.exists():
      continue
    if not path.is_file() or path.stat().st_size > 65536:
      raise ValueError('Unknown recovery record')
    value = json.loads(path.read_text())  # Corrupt state blocks cleanup, never guesses rollback.
    if not isinstance(value, dict):
      raise ValueError('Unknown recovery record')
    if name.endswith('status.json') and value.get('state') not in (
        'installed', 'display_only_installed', 'rolled_back', 'applied', 'recovered', 'rejected'):
      raise RuntimeError('Unresolved install status; cleanup refused')
    protected.update(match[:12] for match in re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])', json.dumps(value)))
  folders = []
  for path in updates.iterdir():
    if not re.fullmatch('hud-[0-9a-f]{12}', path.name) or path.is_symlink() or not path.is_dir():
      continue
    folders.append(path)
  # Keep two newest uploads even if installation never reached a status record.
  folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
  protected.update(path.name[4:] for path in folders[:2])
  candidates = []
  now = time.time() if now is None else now  # noqa: TID251 -- compare filesystem wall-clock mtimes
  for path in folders:
    if path.name[4:] in protected:
      continue
    entries = list(path.iterdir())
    if not entries or not {p.name for p in entries} <= FILES:
      continue  # Includes every AI result/plan, subdirectory and unknown operator file.
    metadata = [p.lstat() for p in entries]
    if any(not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_dev != updates.stat().st_dev for s in metadata):
      continue
    if now - max([path.stat().st_mtime] + [s.st_mtime for s in metadata]) < 86400:
      continue
    report = path / 'nexo-hud-repair.json'
    if not report.is_file() or report.stat().st_size > 65536:
      continue
    try:
      commit = json.loads(report.read_text()).get('source_commit')
    except (ValueError, AttributeError):
      continue
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit) or commit[:12] != path.name[4:]:
      continue
    candidates.append({'folder': path.name, 'allocated_bytes': sum(s.st_blocks * 512 for s in metadata)})
  return sorted(candidates, key=lambda item: (updates / item['folder']).stat().st_mtime)


def reclaim(root, incoming, upload_bytes, *, apply=False):
  if type(upload_bytes) is not int or not 0 <= upload_bytes <= 1 << 30:
    raise ValueError('Invalid HUD upload size')
  candidates = inventory(root, incoming)
  before = shutil.disk_usage(root).free
  required = MIN_FREE + upload_bytes + (64 << 20)
  result = {'free_before_bytes': before, 'required_bytes': required, 'candidates': candidates,
            'reclaimable_bytes': sum(item['allocated_bytes'] for item in candidates), 'deleted': []}
  if apply and before < required and before + result['reclaimable_bytes'] >= required:
    if not shutil.rmtree.avoids_symlink_attacks:
      raise RuntimeError('Safe directory operations unavailable')
    # Flat allowlist only: no rmtree, recursion, link following or service operations.
    for item in candidates:
      if shutil.disk_usage(root).free >= required:
        break
      if item not in inventory(root, incoming):
        raise RuntimeError('HUD upload changed during cleanup')
      path = root / 'updates' / item['folder']
      fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
      try:
        entries = os.listdir(fd)
        if not set(entries) <= FILES or any(
            not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode) for name in entries):
          raise RuntimeError('HUD upload changed during cleanup')
        for name in entries:
          os.unlink(name, dir_fd=fd)
      finally:
        os.close(fd)
      path.rmdir()
      result['deleted'].append(item['folder'])
  result['free_after_bytes'] = shutil.disk_usage(root).free
  result['reclaimed_bytes'] = max(0, result['free_after_bytes'] - before)
  result['ready'] = result['free_after_bytes'] >= required
  return result


def main():
  # Called over authenticated SSH by the existing parked-only Comma updater.
  root = Path('/opt/carrot-jetlink')
  guard = runpy.run_path(str(root / 'updater/nexo_protected.py'))
  guard['storage_guard']()
  with Path('/run/carrot-jetlink-update.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result = reclaim(root, sys.argv[1], int(sys.argv[2]), apply=sys.argv[3:] == ['--apply'])
    print(json.dumps(result), flush=True)
    if not result['ready']:
      raise RuntimeError('DATA_SPACE: insufficient free DATA; protected files retained')


if __name__ == '__main__':
  main()
