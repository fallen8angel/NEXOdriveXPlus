"""Independent USB/Wi-Fi status leases for the existing Jetson badge."""
import math


class JetsonConnectivity:
  def __init__(self):
    self.seen = {}

  def update(self, payload, now):
    if not isinstance(payload, dict) or payload.get('magic') != 'NEXO_JETSON_STATUS':
      return
    # A disconnected USB owner must not clear a still-live legacy Wi-Fi beacon.
    transport = payload.get('transport', 'wifi')
    if transport not in ('usb', 'wifi'):
      return
    key = 'usb_connected' if transport == 'usb' else 'comma_tcp'
    self.seen[transport] = now if payload.get(key) is True else None

  def connected(self, now):
    return any(stamp is not None and math.isfinite(stamp) and 0 <= now - stamp < 2.5 for stamp in self.seen.values())
