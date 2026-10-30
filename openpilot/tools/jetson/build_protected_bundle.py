"""Export committed NEXO sources and a pinned Carrot server for the protected SD runtime."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

CARROT_REV = 'd0dc96fa32410403dfb2fefff3e2909c11ec109b'
PATHS = ['LICENSE', 'opendbc_repo/LICENSE', 'opendbc_repo/opendbc/car/car.capnp',
         'opendbc_repo/opendbc/car/include', 'openpilot/__init__.py', 'openpilot/cereal',
         'openpilot/common', 'openpilot/tools/jetson', 'openpilot/selfdrive/carrot/cluster',
         'openpilot/selfdrive/carrot/carrot_navi.py', 'openpilot/selfdrive/carrot/carrot_navi_cereal.py',
         'openpilot/selfdrive/controls/lib/cutin_alert.py', 'openpilot/selfdrive/controls/lib/cutin_helpers.py',
         'openpilot/selfdrive/assets', 'openpilot/system/hardware', 'openpilot/system/version.py',
         'openpilot/selfdrive/modeld/constants.py', 'openpilot/selfdrive/modeld/jetlink',
         'openpilot/selfdrive/modeld/models/driving_supercombo.onnx']
VENDOR_PATHS = ['LICENSE', 'third_party/jetlink', 'tools/jetlink']
MODEL = 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
HUD_ASSETS = {'KaiGenGothicKR-Bold.ttf', 'JetBrainsMono-Medium.ttf', 'speed_bg.png',
              'traffic_red.png', 'traffic_green.png', 'carrot_cruse_gap_trimmed.png',
              'carrot_wheel_org.png', 'carrot_wheel_lane.png', 'wifi_strength_full.png'}


def git(root, *args):
  return subprocess.check_output(['git', '-C', str(root), *args])


def committed_archive(root, paths, expected=None):
  revision = git(root, 'rev-parse', 'HEAD').decode().strip()
  if expected is not None and revision != expected:
    raise ValueError('Carrot server must match the pinned revision')
  git(root, 'diff', '--exit-code', 'HEAD', '--', *paths)
  if git(root, 'ls-files', '--others', '--exclude-standard', '--', *paths).strip():
    raise ValueError('Commit host sources before exporting')
  return revision, git(root, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'archive', 'HEAD', *paths)


def add_bytes(target, name, value):
  item = tarfile.TarInfo(name)
  item.size, item.mode = len(value), 0o644
  target.addfile(item, io.BytesIO(value))


def add_archive(target, archive, prefix='', compact=False):
  with tarfile.open(fileobj=io.BytesIO(archive)) as source:
    for item in source:
      if not (item.isdir() or item.isfile()):
        raise ValueError('Host bundle cannot contain links or special files')
      if compact and item.isfile():
        if item.name.startswith('openpilot/selfdrive/assets/') and Path(item.name).name not in HUD_ASSETS:
          continue
        if item.name.startswith('openpilot/system/hardware/') and not item.name.endswith('.py'):
          continue
      item.name = prefix + item.name
      item.uid = item.gid = 0
      item.uname = item.gname = ''
      item.mode = 0o755 if item.isdir() else 0o644
      target.addfile(item, source.extractfile(item) if item.isfile() else None)


def launcher(module, hud=False):
  lines = ['from pathlib import Path', 'import os, sys',
           'root = Path(__file__).resolve().parents[2]', 'sys.path.insert(0, str(root))',
           "os.environ['CARROT_JETSON'] = str(root / 'carrot-server')",
           "os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')",
           "os.environ.setdefault('OMP_NUM_THREADS', '1')"]
  if hud:
    lines.append("sys.argv.insert(1, '--native-jetlink')")
  lines += [f'from {module} import main', 'raise SystemExit(main())', '']
  return '\n'.join(lines).encode()


def vendor_launcher(script):
  # APP's Xorg unit keeps this path across DATA release switches. The pinned
  # vendor helper has top-level code (no main), so execute it as a script.
  return ('from pathlib import Path\nimport runpy\n'
          'root = Path(__file__).resolve().parents[2]\n'
          f"runpy.run_path(str(root / 'carrot-server/tools/jetlink/{script}'), run_name='__main__')\n").encode()


def build(root, vendor, output, compact=False):
  revision, archive = committed_archive(root, PATHS)
  carrot_revision, carrot_archive = committed_archive(vendor, VENDOR_PATHS, CARROT_REV)
  # Derive the contract with the same pure-Python parser as the server. No
  # vehicle extensions, GPU imports, or alternate upstream model are involved.
  parser = vendor / 'third_party/jetlink'
  import sys
  sys.path.insert(0, str(parser))
  from jetlink.spec import spec_from_onnx
  spec = spec_from_onnx(str(root / MODEL), frame_skip=4).to_dict()
  from openpilot.tools.jetson.onnx_inputs import preflight
  validation = preflight(root / MODEL, spec['sha256'])
  from openpilot.tools.jetson.trt_diagnostics import analyze_model
  build_profile = analyze_model(root / MODEL)
  marker = {'format': 1, 'source_commit': revision, 'carrot_commit': carrot_revision,
            'runtime': {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'}, 'model': spec}
  output.parent.mkdir(parents=True, exist_ok=True)
  with tarfile.open(output, 'w:gz') as target:
    add_archive(target, archive, compact=compact)
    add_archive(target, carrot_archive, 'carrot-server/')
    add_bytes(target, 'SOURCE_COMMIT', (revision + '\n').encode())
    add_bytes(target, 'NEXO_RUNTIME.json', json.dumps(marker, indent=2).encode())
    add_bytes(target, 'tools/jetlink/server.py', launcher('openpilot.tools.jetson.native_host'))
    add_bytes(target, 'tools/jetlink/hud.py', launcher('openpilot.tools.jetson.hud', hud=True))
    add_bytes(target, 'tools/jetlink/boot_display.py', launcher('openpilot.tools.jetson.boot_display'))
    add_bytes(target, 'tools/jetlink/xorg_auth.py', vendor_launcher('xorg_auth.py'))
  # Check the exported bytes, not the working tree or an installed old release.
  from openpilot.tools.jetson.runtime_audit import audit_archive
  audit = audit_archive(output)
  identity = hashlib.sha256(output.read_bytes()).hexdigest()
  report = {**marker, 'bundle': output.name, 'bundle_sha256': identity, 'bundle_size': output.stat().st_size,
            'onnx_preflight': validation, 'tensorrt_build_profile': build_profile, 'runtime_audit': audit}
  output.with_name(output.name.removesuffix('.tar.gz') + '.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
  print(json.dumps({'source_commit': revision, 'bundle_sha256': identity, 'bundle_size': output.stat().st_size}))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--carrot', type=Path, required=True)
  parser.add_argument('--compact', action='store_true', help='Only HUD assets and Python hardware adapters; same NEXO model')
  parser.add_argument('output', type=Path)
  args = parser.parse_args()
  build(Path(__file__).resolve().parents[3], args.carrot.resolve(), args.output.resolve(), args.compact)


if __name__ == '__main__':
  main()
