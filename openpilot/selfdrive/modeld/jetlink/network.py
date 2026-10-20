"""Recover the existing comma gadget NCM network, independent of inference.

The worker provisions only the verified NCM NIC and the Jetson peer route.
Never creates/rebinds gadgets, changes UDC/ADB, or enables Jetlink inference.
The older explicit offroad link-only helper is retained for compatibility.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import time

from openpilot.selfdrive.modeld.jetlink import enabled
from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm

USB0 = Path('/sys/class/net/usb0')


def _read(path):
  try:
    return path.read_text().strip(), ''
  except OSError as exc:
    return '', str(exc)[:240]


def usb0_state():
  """Keep administrative state separate from physical/operational state."""
  net, discovery_error = USB0, ''
  try:
    if ncm.transport_mode() == 'ncm-tcp':
      net = ncm.NET_CLASS / ncm.comma_interface()
  except (OSError, RuntimeError, ValueError) as exc:
    net, discovery_error = ncm.NET_CLASS / '_missing_ncm_', str(exc)[:300]
  flags_text, _ = _read(net / 'flags')
  try:
    flags = int(flags_text, 16)
  except ValueError:
    flags = None
  admin_up = bool(flags & 0x1) if flags is not None else None  # IFF_UP
  operstate, _ = _read(net / 'operstate')
  carrier_text, carrier_error = _read(net / 'carrier')
  carrier = {'1': True, '0': False}.get(carrier_text)
  source = 'carrier' if carrier is not None else 'unknown'
  # Reading carrier returns EINVAL while administratively DOWN on comma4.
  if admin_up is False:
    carrier, source = False, 'admin_down'
  elif carrier is None and flags is not None:
    carrier, source = bool(flags & 0x10000), 'flags_lower_up'  # IFF_LOWER_UP
  elif carrier is None and operstate in ('up', 'down', 'lowerlayerdown'):
    carrier, source = operstate == 'up', 'operstate'
  return {'usb_present': net.exists(), 'usb_admin_up': admin_up,
          'usb_interface': net.name if not discovery_error else '', 'usb_discovery_error': discovery_error,
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
  if usb0_state().get('usb_interface') != state['usb_interface'] or not offroad_link_allowed(params):
    return 'skipped', ''
  command = ['ip', 'link', 'set', 'dev', state['usb_interface'], 'up']
  if os.geteuid() != 0:
    command = ['sudo', '-n', *command]
  try:
    subprocess.run(command, check=True, timeout=2, capture_output=True)
  except (OSError, subprocess.SubprocessError) as exc:
    return 'error', str(exc)[:240]
  return 'up', ''


def ensure_ncm_network():
  """One retryable pass; no carrier, inference marker or transport file required."""
  try:
    iface = ncm.comma_interface()
    index = socket.if_nametoindex(iface)
  except (OSError, RuntimeError, ValueError) as exc:
    return 'waiting_ncm', str(exc)[:300]

  def same_device():
    if ncm.comma_interface() != iface or socket.if_nametoindex(iface) != index:
      raise RuntimeError('NCM interface changed during network setup; rediscover next pass')

  def mutate(*args):
    same_device()  # Revalidate the bound NCM function/ifindex before every write.
    command = ['ip', *args]
    if os.geteuid() != 0:
      command = ['sudo', '-n', *command]
    subprocess.run(command, check=True, timeout=2, capture_output=True)
    same_device()

  def addresses():
    rows = ncm.ip_json('addr', 'show', 'dev', iface)
    if len(rows) != 1 or rows[0].get('ifname') != iface or rows[0].get('ifindex') != index:
      raise RuntimeError('NCM address inspection no longer matches the discovered interface')
    return rows[0]

  def peer_routes():
    routes = ncm.ip_json('route', 'show', 'exact', ncm.JETSON_ADDR + '/32')
    if routes and (len(routes) != 1 or routes[0].get('dev') != iface or routes[0].get('gateway')
                   or routes[0].get('type', 'unicast') != 'unicast'
                   or routes[0].get('prefsrc', ncm.COMMA_ADDR) != ncm.COMMA_ADDR
                   or routes[0].get('multipath') or routes[0].get('nhid')):
      raise RuntimeError('Conflicting Jetson host route; refusing to overwrite another network')
    return routes

  try:
    row = addresses()
    if any(a.get('local') == ncm.COMMA_ADDR and a.get('prefixlen') != 24 for a in row.get('addr_info', [])):
      raise RuntimeError('Existing comma NCM address has an unexpected prefix; refusing to replace it')
    peer_routes()  # Detect a foreign host route before any mutation.
    if 'UP' not in row.get('flags', []):
      mutate('link', 'set', 'dev', iface, 'up')
    row = addresses()
    if not any(a.get('local') == ncm.COMMA_ADDR and a.get('prefixlen') == 24 for a in row.get('addr_info', [])):
      # Suppress the automatic /24 route: only .250/32 should use USB. An
      # already provisioned address/connected route belongs to its original owner.
      mutate('addr', 'add', ncm.COMMA_ADDR + '/24', 'dev', iface, 'noprefixroute')
    if not peer_routes():
      mutate('route', 'add', ncm.JETSON_ADDR + '/32', 'dev', iface, 'src', ncm.COMMA_ADDR, 'proto', '186')
    same_device()
    if _read(ncm.NET_CLASS / iface / 'carrier')[0] != '1':
      return 'waiting_usb', ''
    ncm.require_usb_route(iface)  # Reuse the transport's strict read-only check.
    return 'ready', ''
  except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
    return 'error', str(exc)[:300]


def main():
  from openpilot.common.swaglog import cloudlog

  previous = None
  while True:
    action, error = ensure_ncm_network()
    if (action, error) != previous:
      if action == 'error':
        cloudlog.warning('NEXO USB-NCM recovery: %s', error)
      else:
        cloudlog.info('NEXO USB-NCM network: %s %s', action, error)
    previous = action, error
    time.sleep(2)


if __name__ == '__main__':
  main()
