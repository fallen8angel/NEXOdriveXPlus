"""Read-only Windows/Linux APP compatibility check; never apply a patch."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re


def read_exact(stream, size):
  result = bytearray()
  while len(result) < size:
    data = stream.read(size - len(result))
    if not data:
      raise ValueError('Short card read')
    result.extend(data)
  return bytes(result)


def validate_index(index):
  profiles = index.get('profiles', [])
  if index.get('format') != 'carrot-offline-boot-v1' or len(profiles) != 2:
    raise ValueError('Unknown compatibility manifest')
  first = profiles[0]
  start, length = first['root_offset'], first['root_bytes']
  if start < 512 or start % 512 or length <= 0 or length % 512 or start + length > first['image_bytes']:
    raise ValueError('Invalid APP bounds')
  patches, previous = [], start
  for item in first['patches']:
    offset = item['offset']
    before, after = [base64.b64decode(item[key], validate=True) for key in ('before', 'after')]
    if (offset < previous or offset % 512 or not before or len(before) % 512 or
        len(before) != len(after) or offset + len(before) > start + length):
      raise ValueError('Invalid normalization extent')
    if any(hashlib.sha256(data).hexdigest() != item[key] for data, key in
           ((before, 'before_sha256'), (after, 'after_sha256'))):
      raise ValueError('Extent checksum mismatch')
    patches.append((offset, before))
    previous = offset + len(before)
  if not patches or sum(len(data) for _, data in patches) > 32 << 20:
    raise ValueError('Unexpected normalization size')
  identities = set()
  for profile in profiles:
    if any(profile[key] != first[key] for key in ('root_offset', 'root_bytes', 'image_bytes', 'partition_guard')):
      raise ValueError('Variant layout mismatch')
    if [(item['offset'], item['before']) for item in profile['patches']] != [(item['offset'], item['before']) for item in first['patches']]:
      raise ValueError('Variant normalization mismatch')
    if not re.fullmatch('[0-9a-f]{64}', profile['root_sha256']) or profile['root_sha256'] in identities:
      raise ValueError('Invalid or ambiguous APP identity')
    identities.add(profile['root_sha256'])
  guard = first['partition_guard']
  expected = base64.b64decode(guard['data'], validate=True)
  if guard['offset'] < 512 or guard['offset'] % 512 or len(expected) != 512 or guard['offset'] + 512 > start:
    raise ValueError('Invalid partition identity')
  return first, patches, expected


def inspect_card(target, index, report=print):
  first, patches, expected = validate_index(index)
  start, length = first['root_offset'], first['root_bytes']
  digest, position, next_report = hashlib.sha256(), start, start
  # No write-capable device handle, volume dismount, mount or patch operation.
  with Path(target).open('rb', buffering=0) as stream:
    stream.seek(first['partition_guard']['offset'])
    if read_exact(stream, 512) != expected:
      raise ValueError('Card partition layout is not supported')
    stream.seek(start)
    while position < start + length:
      block = bytearray(read_exact(stream, min(4 << 20, start + length - position)))
      for offset, original in patches:
        low, high = max(offset, position), min(offset + len(original), position + len(block))
        if low < high:
          block[low-position:high-position] = original[low-offset:high-offset]
      digest.update(block)
      position += len(block)
      if position >= next_report:
        report(f'APP_READ {position-start}/{length}')
        next_report = position + (1 << 30)
  actual = digest.hexdigest()
  matches = [profile for profile in index['profiles'] if profile['root_sha256'] == actual]
  return {'state': 'supported_app_profile' if len(matches) == 1 else 'unsupported_app_profile',
          'profile': matches[0]['release'] if len(matches) == 1 else None,
          'normalized_app_sha256': actual, 'card_written': False, 'installation_started': False,
          'note': 'APP compatibility only; DATA health, network fault and NEXO activation are not verified.'}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--target', required=True)
  parser.add_argument('--manifest', required=True)
  parser.add_argument('--manifest-sha256', required=True)
  args = parser.parse_args()
  data = Path(args.manifest).read_bytes()
  if len(data) > 4 << 20 or hashlib.sha256(data).hexdigest() != args.manifest_sha256:
    raise ValueError('Compatibility manifest checksum mismatch')
  result = inspect_card(args.target, json.loads(data))
  print('CARD_CHECK_RESULT_JSON=' + json.dumps(result), flush=True)
  return 0 if result['state'] == 'supported_app_profile' else 2


if __name__ == '__main__':
  raise SystemExit(main())
