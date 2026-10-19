"""Read-only, bounded memory observations for existing Jetson diagnostics.

No smaps walks, subprocesses, resource reclamation, USB writes or process control.
"""
from collections import deque
import os
from pathlib import Path
import time

ROLES = (('yolo', ('orin_yolo',)), ('bridge', ('tools.jetson.host', 'messaging/bridge', 'orin_frame_bridge')),
         ('hud', ('tools.jetson.hud', 'cluster_autorun', 'cluster_run', 'hud.py')),
         ('jetlink', ('jetlink.daemon', 'jetlink/server', 'tools.jetson.native_host')),
         ('beacon', ('jetson_status_beacon',)), ('modeld', ('modeld',)), ('proclogd', ('proclogd',)))


class MemoryObserver:
  INTERVAL = 5.

  def __init__(self, proc=Path('/proc')):
    self.proc = Path(proc)
    self.samples = deque(maxlen=61)  # At most five minutes, independent of uptime/PID churn.
    self.cached = {}
    self.last = None

  def sample(self, now=None):
    now = time.monotonic() if now is None else now
    if self.last is not None and 0 <= now - self.last < self.INTERVAL:
      return self.cached
    try:
      memory = {}
      for line in (self.proc / 'meminfo').read_text().splitlines():
        key, _, raw = line.partition(':')
        if key in ('MemTotal', 'MemAvailable'):
          memory[key] = int(raw.split()[0]) * 1024
      total, available = memory['MemTotal'], memory['MemAvailable']
      if not 0 <= available <= total or not total:
        raise ValueError('invalid meminfo')
      page_size = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
      processes = []
      for count, entry in enumerate(self.proc.iterdir()):
        if count >= 4096 or len(processes) >= 32:
          break
        if not entry.name.isdigit():
          continue
        try:
          with (entry / 'cmdline').open('rb') as source:
            command = source.read(1024).replace(b'\0', b' ').decode(errors='replace')
          role = next((role for role, patterns in ROLES if any(p in command for p in patterns)), None)
          if role is None:
            continue
          stat = (entry / 'stat').read_text()
          fields = stat[stat.rfind(')') + 2:].split()
          processes.append({'pid': int(entry.name), 'starttime': int(fields[19]),
                            'role': role, 'rss_bytes': max(0, int(fields[21])) * page_size})
        except (OSError, ValueError, IndexError):
          continue  # Processes may exit during this observation.
      used = total - available
      current = {(p['pid'], p['starttime']): p['rss_bytes'] for p in processes}
      self.samples.append((now, used, current))
      first_time, first_used, baseline = self.samples[0]
      for process in processes:
        previous = baseline.get((process['pid'], process['starttime']))
        process['growth_bytes'] = None if previous is None else process['rss_bytes'] - previous
      self.cached = {'available': True, 'total_bytes': total, 'available_bytes': available,
                     'used_bytes': used, 'growth_bytes': used - first_used,
                     'window_s': now - first_time, 'processes': processes}
    except (OSError, ValueError, KeyError):
      self.samples.clear()
      self.cached = {'available': False}
    self.last = now
    return self.cached


def usb_power_observation(root=Path('/')):
  """Observe the charger vote; electrical topology/compatibility remains unknown."""
  paths = {'power_role': 'sys/class/usbpd/usbpd0/current_pr',
           'data_role': 'sys/class/usbpd/usbpd0/current_dr',
           'force_active': 'sys/kernel/debug/pmic-votable/USB_ICL/force_active',
           'force_val': 'sys/kernel/debug/pmic-votable/USB_ICL/force_val'}
  result = {'mode': 'diagnostic_only', 'hardware_verified': False}
  for key, path in paths.items():
    try:
      with (Path(root) / path).open() as source:
        result[key] = source.read(64).strip()
    except OSError:
      result[key] = None
  return result


memory_observer = MemoryObserver()
