"""Apply only a SHA-bound, same-size R2 APP hook under the Windows volume locks.

The launcher stages the verified bundle to SETUP first. No GPT, filesystem
allocation, bootloader or DATA block is written by this helper.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protected_card_check import inspect_card, read_exact, validate_index


def validate(value):
  if value.get('format') != 'nexo-offline-r2-v1' or value.get('profile') != 'r2-original':
    raise ValueError('Unsupported offline patch')
  first, patches, _ = validate_index(value['compatibility'])
  item = value['patch']
  before, after = [base64.b64decode(item[key], validate=True) for key in ('before', 'after')]
  if (len(patches) != 1 or (item['offset'], before) != patches[0] or len(before) != 6144 or len(after) != len(before) or
      first['release'] != value['profile']):
    raise ValueError('Patch is outside the known image-setup allocation')
  for data, key in ((before, 'before_sha256'), (after, 'after_sha256')):
    if hashlib.sha256(data).hexdigest() != item[key]:
      raise ValueError('Patch bytes checksum differs')
  compile(after, '<offline-hook>', 'exec')
  return item['offset'], before, after


def apply(target, value, backup, report=print):
  offset, before, after = validate(value)
  checked = inspect_card(target, value['compatibility'], report)
  if checked['state'] != 'supported_app_profile' or checked['profile'] != value['profile']:
    raise ValueError('Card APP profile differs; nothing written')
  # Backup only the known changed APP bytes; no identity or private keys.
  record = json.dumps({'offset': offset, 'before': base64.b64encode(before).decode(),
                       'before_sha256': hashlib.sha256(before).hexdigest()}).encode()
  if backup.exists():
    if backup.read_bytes() != record:
      raise ValueError('Existing APP recovery file differs; nothing written')
  with Path(target).open('r+b', buffering=0) as stream:
    # Locked target layout is checked a second time before the first write.
    first = value['compatibility']['profiles'][0]
    guard = first['partition_guard']
    stream.seek(guard['offset'])
    if read_exact(stream, 512) != base64.b64decode(guard['data'], validate=True):
      raise ValueError('Card partition guard changed; nothing written')
    stream.seek(offset)
    current = read_exact(stream, len(before))
    # A torn retry may contain original/replacement bytes. A different Carrot
    # or arbitrary helper is refused even though normalization masks this file.
    if any(byte not in (original, replacement) for byte, original, replacement in zip(current, before, after, strict=True)):
      raise ValueError('Existing first-boot helper differs; nothing written')
    if not backup.exists():
      with backup.open('xb') as recovery:
        recovery.write(record)
        recovery.flush()
        os.fsync(recovery.fileno())
    if current != after:
      stream.seek(offset)
      if stream.write(after) != len(after):
        raise OSError('Short APP patch write; rerun this same patch before boot')
      stream.flush()
      os.fsync(stream.fileno())
    stream.seek(offset)
    if read_exact(stream, len(after)) != after:
      raise OSError('APP readback differs; rerun this same patch before boot')
  result = {'state': 'pc_patch_verified', 'source_commit': value['source_commit'],
            'app_bytes': len(after), 'installation_started': False, 'jetson_boot_verified': False}
  report('NEXO_CARD_PATCH_RESULT=' + json.dumps(result))
  return result


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--target', required=True)
  parser.add_argument('--manifest', required=True, type=Path)
  parser.add_argument('--manifest-sha256', required=True)
  parser.add_argument('--backup', required=True, type=Path)
  args = parser.parse_args()
  raw = args.manifest.read_bytes()
  if len(raw) > 4 << 20 or hashlib.sha256(raw).hexdigest() != args.manifest_sha256:
    raise ValueError('Offline manifest checksum differs')
  apply(args.target, json.loads(raw), args.backup)


if __name__ == '__main__':
  main()
