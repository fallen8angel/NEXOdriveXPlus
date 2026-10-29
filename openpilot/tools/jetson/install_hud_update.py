"""Apply an offline HUD repair using the existing protected Display Only config.

Copy alongside the matching protected_install.py/protected_ncm.py and bundle.
This invokes the existing DATA installer; it does not edit APP or SETUP.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

CONFIG = Path('/opt/carrot-jetlink/updates/nexo-ncm.json')


def install_command(directory, bundle, sha256, config=CONFIG):
  value = json.loads(config.read_text())
  if value.get('display_only') is not True or value.get('transport') != 'ncm-tcp':
    raise ValueError('An existing protected NCM Display Only configuration is required')
  interface, model = value.get('interface'), value.get('model_sha256')
  if not isinstance(interface, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', interface) or interface in ('.', '..'):
    raise ValueError('Invalid installed interface')
  if not isinstance(model, str) or not re.fullmatch('[0-9a-f]{64}', model):
    raise ValueError('Invalid installed model hash')
  if not re.fullmatch('[0-9a-f]{64}', sha256) or hashlib.sha256(bundle.read_bytes()).hexdigest() != sha256:
    raise ValueError('Bundle SHA256 does not match the supplied checksum')
  for name in ('protected_install.py', 'protected_ncm.py'):
    if not (directory / name).is_file():
      raise ValueError(f'Matching {name} must be alongside this script')
  return [sys.executable, str(directory / 'protected_install.py'), 'install', '--bundle', str(bundle),
          '--sha256', sha256, '--ncm-interface', interface, '--model-sha256', model,
          '--display-only', '--confirmed-offroad']


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--bundle', type=Path, required=True)
  parser.add_argument('--sha256', required=True)
  parser.add_argument('--check-only', action='store_true')
  parser.add_argument('--confirmed-offroad', action='store_true')
  args = parser.parse_args()
  if not args.check_only and not args.confirmed_offroad:
    parser.error('Park the vehicle/offroad and explicitly pass --confirmed-offroad')
  command = install_command(Path(__file__).resolve().parent, args.bundle.resolve(), args.sha256)
  if args.check_only:
    print(json.dumps({'command': command, 'hardware_verified': False}, ensure_ascii=False))
    return 0
  return subprocess.call(command)


if __name__ == '__main__':
  raise SystemExit(main())
