"""DATA-only boot-screen adapter; APP's recovery screen remains untouched."""
import base64
from pathlib import Path
import signal
import sys

from openpilot.tools.jetson.display_owner import hud_requested, panel_owner
from openpilot.tools.jetson.snapshot import read_snapshot
from openpilot.tools.jetson.state import RUNTIME, atomic_json, read_fresh

MIRROR = RUNTIME / 'panel-settings.json'


def mirror_mode():
  value = read_snapshot()
  if value is not None:
    try:
      mode = int(base64.b64decode(value['params'].get('ClusterHudMirror', ''), validate=True) or b'0') & 1
      atomic_json(MIRROR, {'updated': value['updated'], 'mirror': mode})
      return mode
    except (OSError, ValueError, KeyError):
      pass
  return int((read_fresh(MIRROR, float('inf')) or {}).get('mirror', 0)) & 1


def panel_frame(frame, mode=None):
  from PIL import Image
  mode = mirror_mode() if mode is None else mode
  # Mirror in landscape coordinates BEFORE portrait upload, like FFmpeg HUD.
  if not (mode & 1):
    frame = frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
  return frame.transpose(Image.Transpose.ROTATE_270)


def diagnostic_panel(original):
  class Panel(original):
    # NEXO's USB class predates the vendor _usbgpu_reset_protected hook.
    # Enforce the boot screen's no-reset policy at the actual methods too.
    def _reset_and_reconnect(self):
      raise RuntimeError('Boot display retries without resetting USB')

    def _handle_frame_error(self, error):
      raise RuntimeError('Boot display write failed; release panel and retry') from error
  return Panel


def prepare():
  """Compose the exact boot path without opening USB (also used by preflight)."""
  root = Path(__file__).resolve().parents[3]
  sys.path.insert(0, str(root / 'openpilot/selfdrive/carrot/cluster'))
  sys.path.insert(0, str(root / 'carrot-server/tools/jetlink'))
  import boot_display as vendor
  import cluster_usb_display as usb
  # Vendor main imports this lazily. Include it in the import preflight too.
  import openpilot.common.usbgpu_bus_lock as bus
  bus.USBGPU_BUS_LOCK_PATH = '/run/carrot-jetlink-display/usb.lock'
  usb.TuringUsbDisplay = diagnostic_panel(usb.TuringUsbDisplay)
  vendor.hud_requested, vendor.panel_owner = hud_requested, panel_owner
  vendor.panel_frame = panel_frame
  render = vendor.render
  vendor.render = lambda status, **kw: render(status, root=root, **kw)
  return vendor


def main():
  vendor = prepare()
  def stop(*_):
    raise SystemExit(0)  # Run vendor finally: close USB before unlocking.
  signal.signal(signal.SIGTERM, stop)
  vendor.main()


if __name__ == '__main__':
  main()
