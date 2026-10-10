"""Launch the unchanged NEXO renderer with process-local read-only adapters.

Based on the snapshot adapter approach in Carrot tools/jetlink/hud.py. NEXO
additions retain SPAS12, local timestamp translation, side/reverse cameras and
the original crop/size settings. No adapters are installed in vehicle processes.
"""
import base64
import json
import os
from pathlib import Path
import sys
import time
import types

from openpilot.tools.jetson.snapshot import SERVICES, PARAMS, MEMORY_PARAMS, read_snapshot
from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite


class DisplayParams:
  def __init__(self, *args, **kwargs):
    self.values = {}
    self.clock_shift = 0.

  def get(self, key, *args, **kwargs):
    value = read_snapshot()
    if value is not None:
      self.values = value.get('params', {})
      self.clock_shift = value['updated'] - value['sent']
    if key not in PARAMS + MEMORY_PARAMS:
      return None
    raw = self.values.get(key)
    decoded = base64.b64decode(raw, validate=True) if raw is not None else None
    if key == 'CarrotNaviDebug' and decoded:
      data = json.loads(decoded)
      if isinstance(data, dict) and finite(data.get('receivedMono')):
        data['receivedMono'] += self.clock_shift
        decoded = json.dumps(data, allow_nan=False).encode()
    return decoded

  def get_bool(self, key, *args, **kwargs):
    return self.get(key) in (b'1', b'True')

  def get_int(self, key, *args, **kwargs):
    try:
      return int(self.get(key) or 0)
    except ValueError:
      return 0

  def get_float(self, key, *args, **kwargs):
    try:
      return float(self.get(key) or 0)
    except ValueError:
      return 0.

  def put_bool_nonblocking(self, key, value):
    if key != 'ClusterHudConnected':
      raise ValueError('HUD parameters are read-only')
    # Only a local display marker. Never write a vehicle Params value.
    if not value:
      (RUNTIME / 'hud-status.json').unlink(missing_ok=True)


def shifted_event(log, raw, shift, expected):
  with log.Event.from_bytes(raw) as reader:
    if reader.which() != expected:
      raise ValueError('HUD service identity mismatch')
    event = reader.as_builder()
  event.logMonoTime = max(0, int(event.logMonoTime + shift * 1e9))
  return event


class RemoteSubMaster:
  def __init__(self, services):
    from openpilot.cereal import log
    self.log = log
    self.services = services
    self.data = {}
    self.updated = dict.fromkeys(services, False)
    self.valid = dict.fromkeys(services, False)
    self.alive = dict.fromkeys(services, False)
    self.recv_time = dict.fromkeys(services, 0.)
    self.logMonoTime = dict.fromkeys(services, 0)
    self.generations = {}
    self.last_received = None
    for name in services:
      event = log.Event.new_message()
      if name in ('can', 'sendcan'):
        event.init(name, 0)
      else:
        event.init(name)
      self.data[name] = event

  def __getitem__(self, key):
    return getattr(self.data[key], key)

  def update(self, timeout=0):
    self.updated = dict.fromkeys(self.services, False)
    value = read_snapshot()
    if value is None:
      self.valid = dict.fromkeys(self.services, False)
      self.alive = dict.fromkeys(self.services, False)
      return
    received = (value['session'], value['updated'])
    if received == self.last_received:
      return
    self.last_received = received
    shift = value['updated'] - value['sent']
    for name in self.services:
      if name not in SERVICES + ('can',):
        continue
      self.valid[name] = value['valid'].get(name) is True
      self.alive[name] = value['alive'].get(name) is True
      self.recv_time[name] = value['received'].get(name, 0.) + shift
      mono = value['mono'].get(name, 0)
      self.logMonoTime[name] = max(0, int(mono + shift * 1e9))
      generation = (value['session'], mono)
      if generation == self.generations.get(name) or name not in value['events']:
        continue
      raw = base64.b64decode(value['events'][name], validate=True)
      self.data[name] = shifted_event(self.log, raw, shift, name)
      self.generations[name] = generation
      self.updated[name] = True


class ParkingSocket:
  def __init__(self):
    self.generation = None

  def receive_event(self):
    from openpilot.cereal import log
    value = read_snapshot()
    if value is None or 'can' not in value['events']:
      return None
    generation = (value['session'], value['mono'].get('can'))
    if generation == self.generation:
      return None
    self.generation = generation
    return shifted_event(log, base64.b64decode(value['events']['can'], validate=True),
                         value['updated'] - value['sent'], 'can')


class VehicleStats:
  """Keep the main HUD CPU/memory/disk figures about Comma, as before."""
  def __init__(self, *args, **kwargs):
    self.sm = RemoteSubMaster(['deviceState'])

  def sample(self, now=None):
    from cluster_system_monitor import SystemStats
    self.sm.update()
    if not self.sm.alive['deviceState'] or not self.sm.valid['deviceState']:
      return SystemStats()
    ds = self.sm['deviceState']
    def percent(value):
      return float(value) if finite(value) and 0 <= value <= 100 else None
    cores = tuple(percent(v) for v in ds.cpuUsagePercent)
    valid = [v for v in cores if v is not None]
    free = percent(ds.freeSpacePercent)
    return SystemStats(cpu_core_percents=cores, cpu_used_percent=sum(valid) / len(valid) if valid else None,
                       memory_used_percent=percent(ds.memoryUsagePercent),
                       disk_used_percent=100 - free if free is not None else None)

  def close(self):
    pass


def shifted_media(log, events, value, now=None):
  if value is None:
    return []
  now = time.monotonic() if now is None else now
  shift = value['updated'] - value['sent']
  return [shifted_event(log, event.to_bytes(), shift, 'carrotNaviMedia') for event in events[:32]
          if 0 <= now - (event.logMonoTime / 1e9 + shift) < 2]


def install_adapters(native=False):
  import openpilot.cereal as cereal
  from openpilot.cereal import messaging as original
  params = types.ModuleType('openpilot.common.params')
  params.Params = DisplayParams
  sys.modules[params.__name__] = params
  import openpilot.common as common
  common.params = params
  messaging = types.ModuleType('openpilot.cereal.messaging')
  messaging.SubMaster = RemoteSubMaster

  def sub_sock(service, **kwargs):
    if service == 'can':
      return ParkingSocket()
    if service == 'carrotNaviMedia':
      if native:
        from hud_navi import RemoteMediaSocket
        return RemoteMediaSocket()
      return original.sub_sock(service, addr='127.0.0.1', **kwargs)
    raise ValueError(f'unsupported HUD subscription: {service}')

  def drain(sock, **kwargs):
    if native:
      # Navigation was produced on Comma's clock, like vehicle snapshots.
      return shifted_media(original.log, sock.drain(), read_snapshot())
    out = []
    # Bound renderer work even during a media burst or missing display.
    for _ in range(32):
      event = original.recv_one_or_none(sock)
      if event is None:
        break
      out.append(event)
    return out

  messaging.sub_sock = sub_sock
  messaging.recv_one_or_none = lambda sock: sock.receive_event()
  messaging.drain_sock = drain
  messaging.log_from_bytes = original.log_from_bytes
  sys.modules[messaging.__name__] = messaging
  cereal.messaging = messaging

  import cluster_live_camera
  from openpilot.tools.jetson.camera import RemoteCamera
  cluster_live_camera.LiveRoadCamera = RemoteCamera
  # This factory override stays in this Jetson renderer process. It does not
  # import VisionIPC or create a local driver camera / DM process on Jetson.
  import cluster_renderer
  cluster_renderer.SystemStatsSampler = VehicleStats
  cluster_renderer.ClusterUiRenderer._create_reverse_camera = lambda self: RemoteCamera(driver=True)


def main():
  import fcntl
  native = '--native-jetlink' in sys.argv
  if native:
    sys.argv.remove('--native-jetlink')
    server_root = Path(os.environ.get('CARROT_JETSON', str(Path.home() / 'carrot-jetson')))
    sys.path.insert(0, str(server_root / 'tools/jetlink'))
  os.environ['ZMQ'] = '1'
  from openpilot.cereal import messaging
  messaging.reset_context()
  root = Path(__file__).resolve().parents[3]
  sys.path.insert(0, str(root / 'openpilot/selfdrive/carrot/cluster'))
  RUNTIME.mkdir(parents=True, exist_ok=True)
  # One process owns the USB panel. A second service fails without resetting it.
  with (RUNTIME / 'hud.lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    install_adapters(native=native)
    import main as cluster
    scan = cluster.find_supported_usb_product
    settings = DisplayParams()
    while (scan(None) is None or read_snapshot() is None or not
           (settings.get_bool('IsOnroad') or settings.get_int('ClusterHudDebug') > 0)):
      time.sleep(.5)

    def require_display(expected):
      product = scan(None)  # Actual supported VID/PIDs, no guessed panel model.
      if product is None:
        raise RuntimeError('HUD USB disconnected; waiting for service restart')
      return product

    cluster.find_supported_usb_product = require_display
    original_display = cluster.TuringUsbDisplay

    class Display(original_display):
      next_status = 0.

      def send_h264_chunk(self, *args, **kwargs):
        value = read_snapshot()
        if value is not None and not (settings.get_bool('IsOnroad') or settings.get_int('ClusterHudDebug') > 0):
          raise SystemExit(0)  # Service waits for the next ignition/debug session.
        result = super().send_h264_chunk(*args, **kwargs)
        now = time.monotonic()
        if now >= self.next_status:
          atomic_json(RUNTIME / 'hud-status.json', {'updated': now, 'pid': os.getpid()})
          self.next_status = now + .5
        return result

    cluster.TuringUsbDisplay = Display
    # Orin Nano uses the existing software encoder; no AGNOS/TICI encoder port.
    sys.argv = [__file__, '--input', 'live', '--output', 'usb', '--fps', '10',
                '--usb-codec', 'h264', '--usb-h264-backend', 'ffmpeg',
                '--usb-h264-ffmpeg-encoder', 'libx264', *sys.argv[1:]]
    cluster.main()


if __name__ == '__main__':
  main()
