"""Audit exported protected runtime bytes, without importing hardware libraries."""
import ast
from pathlib import Path
import tarfile

REQUIRED = (
  'openpilot/__init__.py', 'openpilot/common/usbgpu_bus_lock.py',
  'openpilot/tools/jetson/protected_probe.py', 'openpilot/tools/jetson/protected_install.py',
  'openpilot/tools/jetson/protected_ncm.py', 'openpilot/tools/jetson/runtime_audit.py',
  'openpilot/tools/jetson/boot_display.py', 'openpilot/tools/jetson/display_owner.py',
  'openpilot/tools/jetson/hud.py', 'openpilot/tools/jetson/native_host.py',
  'openpilot/tools/jetson/hud_encoder.py', 'openpilot/tools/jetson/hud_tx.py',
  'openpilot/tools/jetson/ncm_display.py', 'openpilot/tools/jetson/ncm_server.py',
  'openpilot/selfdrive/carrot/cluster/main.py',
  'openpilot/selfdrive/carrot/cluster/cluster_renderer.py',
  'openpilot/selfdrive/carrot/cluster/cluster_usb_display.py',
  'openpilot/selfdrive/carrot/cluster/.vendor/turing-smart-screen-python-main/library/lcd/lcd_comm_turing_usb.py',
  'tools/jetlink/server.py', 'tools/jetlink/hud.py', 'tools/jetlink/boot_display.py', 'tools/jetlink/xorg_auth.py',
  'carrot-server/tools/jetlink/server.py', 'carrot-server/tools/jetlink/boot_display.py',
  'carrot-server/tools/jetlink/boot_status.py', 'carrot-server/tools/jetlink/display_owner.py',
  'carrot-server/tools/jetlink/hud_navi.py', 'carrot-server/tools/jetlink/xorg_auth.py',
  'carrot-server/third_party/jetlink/jetlink/protocol.py',
)


def audit_files(files):
  missing = sorted(set(REQUIRED) - files.keys())
  if missing:
    raise ValueError('Missing protected runtime files: ' + ', '.join(missing))
  modules, imports = 0, set()
  for name, raw in sorted(files.items()):
    if not name.endswith('.py') or '/tests/' in name or Path(name).name.startswith('test_'):
      continue
    # The target uses Python 3.10, even when the packaging PC uses a newer one.
    tree = ast.parse(raw, filename=name, feature_version=(3, 10))
    modules += 1
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        imports.update(alias.name for alias in node.names)
      elif isinstance(node, ast.ImportFrom) and node.module:
        imports.add('.' * node.level + node.module)
  return {'required_files': len(REQUIRED), 'python_files_parsed': modules,
          'imports': sorted(imports), 'target_python': '3.10',
          'target_imports_verified': False, 'hardware_verified': False}


def audit_tree(root):
  return audit_files({p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*.py')})


def audit_archive(path):
  with tarfile.open(path, 'r:gz') as source:
    return audit_files({m.name: source.extractfile(m).read() for m in source
                        if m.isfile() and m.name.endswith('.py')})
