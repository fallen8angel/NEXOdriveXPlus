"""Bind a compact committed runtime to the known R2 first-boot allocation.

Only the verified original R2 APP profile is supported. The publisher reads the
trusted Carrot compatibility index and a NEXO runtime; no device is opened.
"""
import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path
import tarfile
import zlib

from openpilot.tools.jetson.protected_card_check import validate_index


def digest(data):
  return hashlib.sha256(data).hexdigest()


def build(index, bundle, hook, output):
  first, patches, _ = validate_index(index)
  if first['release'] != 'r2-original' or len(patches) != 1 or len(patches[0][1]) != 6144:
    raise ValueError('Only the original R2 single first-boot allocation is supported')
  blob = bundle.read_bytes()
  if not 0 < len(blob) < index['setup_bytes'] - (512 << 10):
    raise ValueError('Compact runtime does not fit SETUP with reserve')
  with tarfile.open(bundle) as package:
    marker = json.load(package.extractfile('NEXO_RUNTIME.json'))
    source = package.extractfile('SOURCE_COMMIT').read().decode().strip()
  if source != marker['source_commit'] or marker['runtime'] != {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'}:
    raise ValueError('Unsupported committed runtime contract')
  offset, before = patches[0]
  text = before.rstrip(b'\x00').decode()
  entry = "if __name__ == '__main__':\n  main()"
  if text.count(entry) != 1 or 'nexo_offline_boot' in text:
    raise ValueError('Unexpected original image helper')
  sha256 = digest(blob)
  hook = hook.replace('NEXO_SHA_PLACEHOLDER', sha256).replace('NEXO_SOURCE_PLACEHOLDER', source).replace(
    '0  # NEXO_BYTES_PLACEHOLDER', str(len(blob)))
  combined = text.replace(entry, '') + '\n' + hook + """
if __name__ == '__main__':
  main()
  try:
    nexo_offline_boot()
  except Exception as error:
    print('NEXO_OFFLINE_BOOT_FAILED', type(error).__name__, flush=True)
    try:
      from protected_storage import setup_mount
      from persistent_state import durable_write
      with setup_mount(writable=True) as identity:
        if identity is not None:
          durable_write(identity.parent / 'NEXO-OFFLINE-RESULT.json', json.dumps(
            {'state': 'bootstrap_failed', 'error_type': type(error).__name__, 'installation_started': False}).encode(), 0o600)
    except Exception:
      pass
"""
  ast.parse(combined, feature_version=(3, 10))
  packed = base64.b85encode(zlib.compress(combined.encode(), 9))
  after = (b'# NEXO optional offline bootstrap; source in openpilot/tools/jetson.\nimport base64,zlib\n'
           + b'exec(compile(zlib.decompress(base64.b85decode(' + repr(packed).encode() + b')),__file__,"exec"))\n')
  if len(after) > len(before):
    raise ValueError('Bootstrap exceeds the existing first-boot file allocation')
  after += b' ' * (len(before) - len(after))
  result = {'format': 'nexo-offline-r2-v1', 'source_commit': source, 'bundle_sha256': sha256,
            'bundle_bytes': len(blob), 'compatibility': index, 'profile': first['release'],
            'patch': {'offset': offset, 'before': base64.b64encode(before).decode(), 'after': base64.b64encode(after).decode(),
                      'before_sha256': digest(before), 'after_sha256': digest(after)}}
  output.write_text(json.dumps(result, separators=(',', ':')) + '\n', encoding='utf-8')
  return result


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--compatibility', type=Path, required=True)
  parser.add_argument('--compatibility-sha256', required=True)
  parser.add_argument('--bundle', type=Path, required=True)
  parser.add_argument('output', type=Path)
  args = parser.parse_args()
  data = args.compatibility.read_bytes()
  if digest(data) != args.compatibility_sha256:
    raise ValueError('Trusted compatibility index differs')
  result = build(json.loads(data), args.bundle, Path(__file__).with_name('offline_boot_hook.py').read_text(), args.output)
  print(json.dumps({key: result[key] for key in ('source_commit', 'bundle_sha256', 'bundle_bytes')}))


if __name__ == '__main__':
  main()
