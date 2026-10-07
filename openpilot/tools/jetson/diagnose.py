"""Read-only Jetson/Comma preflight. Does not install drivers or change USB roles."""
import argparse
import importlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

from openpilot.tools.jetson.state import STATUS, RUNTIME, read_fresh


def command(args):
  try:
    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    return {'returncode': result.returncode, 'output': (result.stdout + result.stderr)[-12000:]}
  except (OSError, subprocess.TimeoutExpired) as exc:
    return {'error': str(exc)}


def preflight(role):
  modules = ['openpilot.cereal.messaging', 'openpilot.common.params']
  if role == 'host':
    modules += ['usb1', 'usb.core', 'numpy', 'PIL', 'pyray', 'av']
  imports = {}
  for name in modules:
    try:
      importlib.import_module(name)
      imports[name] = 'ok'
    except Exception as exc:
      imports[name] = f'{type(exc).__name__}: {exc}'
  report = {'role': role, 'platform': platform.platform(), 'imports': imports,
            'link': read_fresh(STATUS), 'hud': read_fresh(RUNTIME / 'hud-status.json'),
            'yolo': read_fresh(RUNTIME / 'yolo.json'), 'ffmpeg': shutil.which('ffmpeg'),
            'usb': command(['lsusb']), 'usb_tree': command(['lsusb', '-t'])}
  for name, path in [('l4t', '/etc/nv_tegra_release'), ('board', '/proc/device-tree/model'),
                     ('usb_role', '/sys/class/power_supply/usb/typec_mode')]:
    try:
      report[name] = Path(path).read_text().replace('\0', '').strip()
    except OSError:
      report[name] = None
  report['imports_ok'] = all(v == 'ok' for v in imports.values())
  return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--role', choices=('host', 'vehicle'), default='host')
  args = parser.parse_args()
  report = preflight(args.role)
  print(json.dumps(report, indent=2))
  return 0 if report['imports_ok'] else 1


if __name__ == '__main__':
  raise SystemExit(main())
