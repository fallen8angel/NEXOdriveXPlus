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
PYC = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*\.cpython-\d+(?:\.opt-[0-2])?\.pyc\Z')


def _checked_pycache(path, device):
  """Only a flat, bounded Python bytecode cache is disposable with a HUD upload."""
  if not path.exists() and not path.is_symlink():
    return []
  entry = path.lstat()
  if not stat.S_ISDIR(entry.st_mode) or entry.st_dev != device:
    return None
  children = list(path.iterdir())
  if len(children) > 32:
    return None
  checks = []
  for child in children:
    meta = child.lstat()
    if (not PYC.fullmatch(child.name) or not stat.S_ISREG(meta.st_mode) or
        meta.st_nlink != 1 or meta.st_dev != device or meta.st_size > 4 << 20):
      return None
    checks.append(meta)
  return checks


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
  now = time.time() if now is None else now
  device = updates.stat().st_dev
  for path in folders:
    if path.name[4:] in protected:
      continue
    entries = list(path.iterdir())
    regular = [p for p in entries if p.name != '__pycache__']
    if not regular or not {p.name for p in entries} <= FILES | {'__pycache__'}:
      continue  # Includes every AI result/plan and unknown operator file.
    metadata = [p.lstat() for p in regular]
    if any(not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_dev != device for s in metadata):
      continue
    cache = path / '__pycache__'
    cache_metadata = _checked_pycache(cache, device)
    if cache_metadata is None:
      continue
    # An interpreter may create __pycache__ much later than the uploaded files.
    # Without a cache, retain the original folder-mtime freshness safeguard.
    ages = [s.st_mtime for s in metadata]
    if not (cache.exists() or cache.is_symlink()):
      ages.append(path.stat().st_mtime)
    if now - max(ages) < 86400:
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
    allocated = sum(s.st_blocks * 512 for s in metadata + cache_metadata)
    if cache.exists():
      allocated += cache.lstat().st_blocks * 512
    candidates.append({'folder': path.name, 'allocated_bytes': allocated})
  return sorted(candidates, key=lambda item: (updates / item['folder']).stat().st_mtime)


def reclaim(root, incoming, upload_bytes, *, apply=False):
  if type(upload_bytes) is not int or not 0 <= upload_bytes <= 1 << 30:
    raise ValueError('Invalid HUD upload size')
  candidates = inventory(root, incoming)
  before = shutil.disk_usage(root).free
  required = MIN_FREE + upload_bytes + (64 << 20)
  result = {'free_before_bytes': before, 'required_bytes': required, 'candidates': candidates,
            'reclaimable_bytes': sum(item['allocated_bytes'] for item in candidates), 'deleted': [],
            'release_candidates': [], 'release_deleted': [], 'release_reclaimable_bytes': 0}
  # Before making ANY deletion require sufficient eligible, explicitly
  # enumerated old uploads + old verified releases. No partial speculative GC.
  if before < required and apply and before + result['reclaimable_bytes'] < required:
    release_candidates = release_inventory(root, incoming)
    result['release_candidates'] = release_candidates
    result['release_reclaimable_bytes'] = sum(item['allocated_bytes'] for item in release_candidates)
  if apply and before < required and (before + result['reclaimable_bytes'] +
       result['release_reclaimable_bytes'] >= required):
    if not shutil.rmtree.avoids_symlink_attacks:
      raise RuntimeError('Safe directory operations unavailable')
    # Flat allowlisted regular files plus a checked, single-level Python bytecode cache.
    for item in candidates:
      if shutil.disk_usage(root).free >= required:
        break
      if item not in inventory(root, incoming):
        raise RuntimeError('HUD upload changed during cleanup')
      path = root / 'updates' / item['folder']
      fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
      try:
        entries = os.listdir(fd)
        normal = [name for name in entries if name != '__pycache__']
        if not normal or not set(entries) <= FILES | {'__pycache__'} or any(
            not stat.S_ISREG((s := os.stat(name, dir_fd=fd, follow_symlinks=False)).st_mode) or
            s.st_nlink != 1 or s.st_dev != os.fstat(fd).st_dev for name in normal):
          raise RuntimeError('HUD upload changed during cleanup')
        if '__pycache__' in entries:
          cfd = os.open('__pycache__', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
          try:
            names = os.listdir(cfd)
            if len(names) > 32 or any(
                not PYC.fullmatch(name) or
                not stat.S_ISREG((s := os.stat(name, dir_fd=cfd, follow_symlinks=False)).st_mode) or
                s.st_nlink != 1 or s.st_dev != os.fstat(cfd).st_dev or s.st_size > 4 << 20
                for name in names):
              raise RuntimeError('Unknown or changed Python bytecode cache; cleanup refused')
            for name in names:
              os.unlink(name, dir_fd=cfd)
          finally:
            os.close(cfd)
          os.rmdir('__pycache__', dir_fd=fd)
        for name in normal:
          os.unlink(name, dir_fd=fd)
      finally:
        os.close(fd)
      path.rmdir()
      result['deleted'].append(item['folder'])
    if shutil.disk_usage(root).free < required:
      release_result = reclaim_old_releases(
        root, incoming, required, apply=True,
        candidates=result['release_candidates'] or None)
      result['release_candidates'] = release_result['candidates']
      result['release_deleted'] = release_result['deleted']
  result['free_after_bytes'] = shutil.disk_usage(root).free
  result['reclaimed_bytes'] = max(0, result['free_after_bytes'] - before)
  result['ready'] = result['free_after_bytes'] >= required
  return result


# Only old, authenticated release trees are eligible for DATA-pressure cleanup.
# Never touch cache/models, engines, N16 experiment exports or rollback versions.
RELEASE = re.compile(r'nexo-[0-9a-f]{40}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
HEX256 = re.compile(r'[0-9a-f]{64}\Z')
RELEASE_KEEP_RECENT = 10
RELEASE_MAX_DELETE = 8
RELEASE_MIN_AGE = 24 * 3600


def _release_allocated(path, device):
  """Conservative bounded disk-accounting; links and unknown files refuse cleanup."""
  total = 0
  entries = 0
  for folder, dirs, files in os.walk(path, topdown=True, followlinks=False):
    entries += 1 + len(dirs) + len(files)
    if entries > 12000:
      return None
    for name in dirs + files:
      item = Path(folder) / name
      meta = item.lstat()
      if (meta.st_dev != device or stat.S_ISLNK(meta.st_mode)
          or os.path.ismount(item)):
        return None
      if name in files and (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1):
        return None
      if name in dirs and not stat.S_ISDIR(meta.st_mode):
        return None
      total += meta.st_blocks * 512
    total += Path(folder).lstat().st_blocks * 512
    if total > 512 << 20:
      return None
  return total


def release_inventory(root, incoming, now=None):
  """Choose only historical verified releases while preserving ALL known rollbacks.

  This is not an arbitrary cache cleaner. It needs the current symlink,
  unambiguous update status, exact source markers and zero transactions.
  """
  if not COMMIT.fullmatch(incoming):
    raise ValueError('Invalid release cleanup commit')
  root = Path(root)
  releases, updates = root / 'releases', root / 'updates'
  if (root.is_symlink() or releases.is_symlink() or not releases.is_dir()
      or updates.is_symlink() or not updates.is_dir()):
    raise ValueError('Unsafe release cleanup directories')
  for name in ('pending.json', 'transaction.json', 'nexo-install-transaction.json'):
    if (updates / name).exists() or (updates / name).is_symlink():
      raise RuntimeError('Pending update or recovery; release cleanup refused')
  link = root / 'current'
  current = link.resolve(strict=True)
  if not link.is_symlink() or current.parent != releases.resolve() or not RELEASE.fullmatch(current.name):
    raise ValueError('Cannot prove protected current release')
  protected = {current.name, 'nexo-' + incoming}
  for name in ('nexo-install-previous.json', 'nexo-install-status.json',
               'status.json', 'previous.json'):
    record = updates / name
    if record.is_symlink():
      raise ValueError('Unsafe rollback record')
    if not record.exists():
      continue
    if not record.is_file() or record.stat().st_size > 65536:
      raise ValueError('Unverifiable rollback record')
    value = json.loads(record.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
      raise ValueError('Unverifiable rollback record')
    if name.endswith('status.json') and value.get('state') not in (
        'installed', 'display_only_installed', 'rolled_back', 'applied', 'recovered', 'rejected'):
      raise RuntimeError('Unresolved install status; release cleanup refused')
    # A previous rollback record may use 'nexo-<SHA>' or a nested source commit.
    for commit in re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])',
                             json.dumps(value)):
      protected.add('nexo-' + commit)
  for entry in root.iterdir():
    if entry.is_symlink():
      dest = entry.resolve(strict=True)
      if dest.parent == releases.resolve():
        protected.add(dest.name)
  folders = []
  for entry in releases.iterdir():
    if entry.is_symlink() or not entry.is_dir() or not RELEASE.fullmatch(entry.name):
      continue
    if entry.stat().st_dev != releases.stat().st_dev:
      raise RuntimeError('Unexpected release filesystem boundary')
    folders.append(entry)
  folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
  protected.update(p.name for p in folders[:RELEASE_KEEP_RECENT])
  now = time.time() if now is None else now
  candidates = []
  for item in reversed(folders):
    if len(candidates) >= RELEASE_MAX_DELETE:
      break
    if item.name in protected or now - item.stat().st_mtime < RELEASE_MIN_AGE:
      continue
    src, digest = item / 'SOURCE_COMMIT', item / '.nexo-bundle-sha256'
    if (src.is_symlink() or digest.is_symlink() or
        not src.is_file() or not digest.is_file() or
        src.stat().st_size > 4096 or digest.stat().st_size > 4096):
      continue
    if src.read_text().strip() != item.name[5:] or not HEX256.fullmatch(digest.read_text().strip()):
      continue
    allocated = _release_allocated(item, releases.stat().st_dev)
    if allocated is not None and allocated > 0:
      candidates.append({'folder': item.name, 'allocated_bytes': allocated})
  return candidates


def reclaim_old_releases(root, incoming, required, *, apply=False, candidates=None):
  """Single bounded pass when old HUD upload pruning cannot meet DATA reserve."""
  before = shutil.disk_usage(root).free
  choices = release_inventory(root, incoming) if candidates is None else candidates
  result = {'candidates': choices, 'deleted': []}
  if (not apply or before >= required or
      before + sum(x['allocated_bytes'] for x in choices) < required):
    return result
  if not shutil.rmtree.avoids_symlink_attacks:
    raise RuntimeError('Secure directory deletion unavailable')
  release_root = (root / 'releases').resolve()
  for entry in choices:
    if shutil.disk_usage(root).free >= required:
      break
    # Each deletion must still be eligible under the protected current state.
    if entry not in release_inventory(root, incoming):
      raise RuntimeError('Release candidate changed during preflight')
    path = root / 'releases' / entry['folder']
    if (path.is_symlink() or path.resolve(strict=True).parent != release_root
        or os.path.ismount(path)):
      raise RuntimeError('Release cleanup boundary changed')
    shutil.rmtree(path)
    result['deleted'].append(entry['folder'])
  return result




def main():
  # Called over authenticated SSH by the existing parked-only Comma updater.
  root = Path('/opt/carrot-jetlink')
  guard = runpy.run_path(str(root / 'updater/nexo_protected.py'))
  guard['storage_guard']()
  with Path('/run/carrot-jetlink-update.lock').open('a') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      print('NEXO_UPDATE_LOCK_BUSY action=storage', file=sys.stderr)
      raise SystemExit(os.EX_TEMPFAIL) from None
    result = reclaim(root, sys.argv[1], int(sys.argv[2]), apply=sys.argv[3:] == ['--apply'])
    print(json.dumps(result), flush=True)
    if not result['ready']:
      raise RuntimeError('DATA_SPACE: insufficient free DATA; protected files retained')


if __name__ == '__main__':
  main()
