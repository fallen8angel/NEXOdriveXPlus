"""Optional offroad usb0 preparation; independent of modeld and vehicle control.

Only raises an existing network interface. Never configures addresses/routes,
rebinds the gadget, or lowers the link when this optional feature is disabled.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from openpilot.selfdrive.modeld.jetlink import enabled

USB0 = Path('/sys/class/net/usb0')


def _read(path):
  try:
    return path.read_text().strip(), ''
  except OSError as exc:
    return '', str(exc)[:240]


def usb0_state():
  """Keep administrative state separate from physical/operational state."""
  flags_text, _ = _read(USB0 / 'flags')
  try:
    flags = int(flags_text, 16)
  except ValueError:
    flags = None
  admin_up = bool(flags & 0x1) if flags is not None else None  # IFF_UP
  operstate, _ = _read(USB0 / 'operstate')
  carrier_text, carrier_error = _read(USB0 / 'carrier')
  carrier = {'1': True, '0': False}.get(carrier_text)
  source = 'carrier' if carrier is not None else 'unknown'
  # Reading carrier returns EINVAL while administratively DOWN on comma4.
  if admin_up is False:
    carrier, source = False, 'admin_down'
  elif carrier is None and flags is not None:
    carrier, source = bool(flags & 0x10000), 'flags_lower_up'  # IFF_LOWER_UP
  elif carrier is None and operstate in ('up', 'down', 'lowerlayerdown'):
    carrier, source = operstate == 'up', 'operstate'
  return {'usb_present': USB0.exists(), 'usb_admin_up': admin_up,
          'usb_admin_state': 'up' if admin_up is True else 'down' if admin_up is False else 'unknown',
          'usb_operstate': operstate or 'unknown', 'usb_carrier': carrier,
          'usb_carrier_source': source, 'usb_carrier_error': carrier_error}


def offroad_link_allowed(params, started=False):
  # Fail closed on missing/unknown Params. The manager's started gate and the
  # worker's fresh IsOnroad/IsOffroad checks both exclude onroad setup.
  try:
    return (not started and enabled() and params.get_bool('IsOffroad')
            and not params.get_bool('IsOnroad') and not params.get_bool('NexoJetsonUsb'))
  except Exception:
    return False


def ensure_usb0_up(params):
  if not offroad_link_allowed(params):
    return 'skipped', ''
  state = usb0_state()
  if not state['usb_present'] or state['usb_admin_up'] is None:
    return 'skipped', ''
  if state['usb_admin_up']:
    return 'already_up', ''
  # Recheck after sysfs reads, immediately before the only mutation.
  if not offroad_link_allowed(params):
    return 'skipped', ''
  command = ['ip', 'link', 'set', 'dev', 'usb0', 'up']
  if os.geteuid() != 0:
    command = ['sudo', '-n', *command]
  try:
    subprocess.run(command, check=True, timeout=2, capture_output=True)
  except (OSError, subprocess.SubprocessError) as exc:
    return 'error', str(exc)[:240]
  return 'up', ''


def main():
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog

  params = Params()
  previous_error = ''
  while True:
    action, error = ensure_usb0_up(params)
    if error and error != previous_error:
      cloudlog.warning('NEXO Jetlink offroad usb0 setup: %s', error)
    elif action == 'up':
      cloudlog.info('NEXO Jetlink offroad usb0 administratively UP')
    previous_error = error
    time.sleep(5)


if __name__ == '__main__':
  main()
