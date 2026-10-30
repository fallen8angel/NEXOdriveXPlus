"""Launch the unchanged NEXO renderer with process-local read-only adapters.

Based on the snapshot adapter approach in Carrot tools/jetlink/hud.py. NEXO
additions retain SPAS12, local timestamp translation, side/reverse cameras and
the original crop/size settings. No adapters are installed in vehicle processes.
"""
import base64
from contextlib import contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import sys
import time
import types

from openpilot.tools.jetson.snapshot import SERVICES, PARAMS, MEMORY_PARAMS, read_snapshot
from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite, read_fresh
from openpilot.tools.jetson import hud_events


class SnapshotUnavailable(ConnectionError):
  """Input expiry is a wait condition, not a failed USB panel."""


class RenderExpired(SnapshotUnavailable):
  """Fresh receiver data cannot make an old rendered frame fresh again."""


class EncoderStalled(RuntimeError):
  """An output failure consumes the bounded recovery budget, unlike input loss."""


RENDER_TIMING = {}


@lru_cache(maxsize=4)
def waiting_frame(width, height, portrait, flip_x=False):
  """No cached vehicle pixels, camera, navigation or speed on an expired input."""
  from PIL import Image, ImageDraw
  w, h = (height, width) if portrait else (width, height)
  frame = Image.new('RGBA', (w, h), (8, 12, 16, 255))
  draw = ImageDraw.Draw(frame)
  draw.text((w // 2, h // 2), 'WAITING FOR VEHICLE DATA', fill=(255, 190, 60, 255), anchor='mm')
  if flip_x:
    frame = frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
  if portrait:
    frame = frame.transpose(Image.Transpose.ROTATE_270)
  # Match the renderer's landscape mirror before portrait upload.
  return frame.tobytes()


def guarded_capture(capture):
  last_mode, last_source, last_gap = None, {}, None
  @contextmanager
  def render(*args, **kwargs):
    nonlocal last_mode, last_source, last_gap
    started = time.monotonic()
    source = read_snapshot()
    with capture(*args, **kwargs) as frame:
      now = time.monotonic()
      elapsed = now - started
      RENDER_TIMING['render_ms'] = elapsed * 1000
      RENDER_TIMING['render_expired'] = elapsed >= .5
      # Check the input used to start this capture, not a newer snapshot that
      # may have arrived while a slow render was in progress.
      fresh = source is not None and 0 <= now - source['updated'] < .5 and read_snapshot() is not None
      mode = 'vehicle' if fresh and elapsed < .5 else 'waiting_data'
      if source is not None:
        last_source = {k: source[k] for k in ('session', 'sent', 'updated', 'hud_link') if k in source}
      if mode != last_mode:
        from openpilot.tools.jetson.health import boot_identity
        link = read_fresh(RUNTIME / 'ncm-display.json', float('inf')) or {}
        if mode == 'waiting_data':
          state = link.get('source_state') or {}
          reason = ('render_expired' if elapsed >= .5 else 'tcp_interrupted' if link.get('state') != 'connected'
                    else 'receiver_unavailable' if now - link.get('updated', 0) >= 2
                    else 'snapshot_producer_stalled' if state.get('reason') == 'snapshot_producer_stalled'
                    else 'hud_receive_delayed')
          last_gap = {'event': 'snapshot_expired', 'reason': reason, 'jetson_mono': now,
                      'jetson_boot_id': boot_identity(), 'source': last_source,
                      'tcp_state': link.get('state'), 'last_transport_error': link.get('last_transport_error'),
                      'last_hud_rx': link.get('last_hud_rx'), 'source_state': state}
        event = {'updated': now, 'pid': os.getpid(), 'mode': mode, 'last_gap': last_gap}
        try:
          atomic_json(RUNTIME / 'hud-input.json', event)
        except OSError:
          pass
        print('HUD input ' + json.dumps(event, separators=(',', ':')), flush=True)
        last_mode = mode
      RENDER_TIMING.update(capture_mono=now, display_mode=mode, input_gap=last_gap,
                           frame_expires_mono=source['updated'] + .5 if mode == 'vehicle' else None)
      if elapsed >= .5:
        # A slow first capture (shader/font initialization) must not restart
        # the encoder forever. Submit a neutral frame, never its old speed.
        _, width, height = frame
        yield bytes((0, 0, 0, 255)) * (width * height), width, height
      elif not fresh:
        _, width, height = frame
        yield waiting_frame(width, height, kwargs.get('portrait_upload', False), kwargs.get('flip_x', False)), width, height
      else:
        yield frame
  return render


def failure_reason(error):
  """The encoder thread wraps callback exceptions; retain the root cause."""
  seen = set()
  while error is not None and id(error) not in seen:
    seen.add(id(error))
    if isinstance(error, hud_events.PanelFailure):
      return error.hud_reason
    if isinstance(error, ImportError):
      return 'renderer_import_failed'
    if isinstance(error, EncoderStalled):
      return 'encoder_output_stalled'
    if isinstance(error, RenderExpired):
      return 'render_expired'
    if isinstance(error, SnapshotUnavailable):
      return 'snapshot_expired'
    if isinstance(error, SystemExit) and error.code == 0:
      return 'hud_disabled'
    if 'display is busy' in str(error) or 'panel ownership unavailable' in str(error):
      return 'panel_busy'
    error = error.__cause__ or error.__context__
  return 'renderer_error'


def wait_for_snapshot(timeout=2., clock=time.monotonic, sleep=time.sleep):
  deadline = clock() + timeout
  value = read_snapshot()
  while value is None and clock() < deadline:
    sleep(.05)
    value = read_snapshot()
  return value


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


def event_from_bytes(raw, schema=None):
  from openpilot.cereal import log
  schema = log.Event if schema is None else schema
  with schema.from_bytes(raw) as reader:
    return reader.as_builder()


def install_adapters(native=False):
  import openpilot.cereal as cereal
  # Native USB receives events from Carrot's ordered media socket. The
  # protected Jetson image deliberately has no vehicle msgq/Params binaries.
  # Only the legacy TCP bridge needs the compiled local messaging runtime.
  original = None
  if not native:
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
      return shifted_media(cereal.log, sock.drain(), read_snapshot())
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
  messaging.log_from_bytes = event_from_bytes if native else original.log_from_bytes
  sys.modules[messaging.__name__] = messaging
  cereal.messaging = messaging

  import cluster_live_camera
  from openpilot.tools.jetson.camera import RemoteCamera
  cluster_live_camera.LiveRoadCamera = RemoteCamera
  # This factory override stays in this Jetson renderer process. It does not
  # import VisionIPC or create a local driver camera / DM process on Jetson.
  import cluster_renderer
  cluster_renderer.ClusterUiRenderer.render_to_rgba_buffer = guarded_capture(
    cluster_renderer.ClusterUiRenderer.render_to_rgba_buffer)
  cluster_renderer.SystemStatsSampler = VehicleStats
  cluster_renderer.ClusterUiRenderer._create_reverse_camera = lambda self: RemoteCamera(driver=True)
  from openpilot.tools.jetson.nexo_experimental_indicator import install as install_exp_indicator
  install_exp_indicator(cluster_renderer)


def hud_requested(value):
  """Use one fresh snapshot for both admission and each panel transmission."""
  if value is None:
    return False
  try:
    params = value['params']
    onroad = base64.b64decode(params.get('IsOnroad', ''), validate=True)
    debug = base64.b64decode(params.get('ClusterHudDebug', ''), validate=True)
    return onroad in (b'1', b'True') or int(debug or b'0') > 0
  except (ValueError, TypeError, KeyError):
    return False


def display_class(original):
  class Display(original):
    next_status = 0.

    def open(self):
      hud_events.record('usb_open')
      try:
        result = super().open()
      except Exception as error:
        raise hud_events.PanelFailure(error, 'usb_open') from error
      hud_events.record('renderer_start')
      return result

    def send_h264_chunk(self, *args, **kwargs):
      value = read_snapshot()
      # Input loss is handled BEFORE encoding by guarded_capture. Arbitrarily
      # dropping an H264 chunk would corrupt the decoder's reference stream.
      # A fresh explicit disable still releases ownership to the boot display.
      if value is not None and not hud_requested(value):
        raise SystemExit(0)
      received = value.get('updated') if value else None
      started = time.monotonic()
      try:
        result = super().send_h264_chunk(*args, **kwargs)
      except Exception as error:
        raise hud_events.PanelFailure(error, 'panel_write') from error
      now = time.monotonic()
      mode = RENDER_TIMING.get('output_mode', RENDER_TIMING.get('display_mode'))
      hud_events.record('waiting_data' if mode == 'waiting_data' else 'streaming')
      if now >= self.next_status:
        try:
          from openpilot.tools.jetson.boot_display import mirror_mode
          mirror_mode()  # Retain presentation settings only, never vehicle data.
          atomic_json(RUNTIME / 'hud-status.json', {'updated': now, 'pid': os.getpid(),
                      'last_panel_send_mono': now, 'physical_hud_verified': False,
                      'snapshot_age_s': started - received if finite(received) else None,
                      'panel_send_ms': (now - started) * 1000, **RENDER_TIMING, 'display_mode': mode})
        except OSError:
          pass  # Optional diagnostics must not tear down a successful panel write.
        self.next_status = now + .5
      return result

  return Display


def run_owned_session(run, owner):
  # Keep the shared panel lock while rendering the safe in-session waiting
  # screen. Explicit disable, actual output failure or service stop releases it.
  hud_events.record('ownership_wait')
  with owner(hud=True) as acquired:
    if not acquired:
      raise RuntimeError('Carrot panel ownership unavailable')
    return run()


def _main():
  import fcntl
  native = '--native-jetlink' in sys.argv
  if native:
    sys.argv.remove('--native-jetlink')
    server_root = Path(os.environ.get('CARROT_JETSON', str(Path.home() / 'carrot-jetson')))
    sys.path.insert(0, str(server_root / 'tools/jetlink'))
  if not native:
    os.environ['ZMQ'] = '1'
    from openpilot.cereal import messaging
    messaging.reset_context()
  root = Path(__file__).resolve().parents[3]
  sys.path.insert(0, str(root / 'openpilot/selfdrive/carrot/cluster'))
  RUNTIME.mkdir(parents=True, exist_ok=True)
  child = '--renderer-child' in sys.argv
  if child:
    sys.argv.remove('--renderer-child')
    import ctypes
    import signal
    parent = os.getppid()
    ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
    if os.getppid() != parent:
      return
    def stop_child(*_):
      raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop_child)
  if not child:
    import signal
    def stop(*_):
      raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    from openpilot.tools.jetson.supervisor import RendererSupervisor
    from cluster_usb_display import find_supported_usb_product
    command = [sys.executable, '-m', 'openpilot.tools.jetson.hud', '--renderer-child']
    if native:
      command.append('--native-jetlink')
    command.extend(sys.argv[1:])
    with (RUNTIME / 'hud.lock').open('w') as lock:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
      supervisor = RendererSupervisor(command, lambda: hud_requested(read_snapshot()),
                                      device_ready=lambda: find_supported_usb_product(None) is not None)
      try:
        while True:
          supervisor.tick()
          time.sleep(.25)
      finally:
        supervisor.close()
    return
  if child:
    hud_events.record('adapter_import')
    install_adapters(native=native)
    import cluster_renderer
    from openpilot.tools.jetson.hud_observations import install as install_observations
    install_observations(cluster_renderer)
    import main as cluster
    scan = cluster.find_supported_usb_product
    def require_display(expected):
      product = scan(None)  # Actual supported VID/PIDs, no guessed panel model.
      if product is None:
        raise hud_events.PanelFailure(RuntimeError('HUD USB disconnected'), 'usb_open')
      return product

    cluster.find_supported_usb_product = require_display
    cluster.TuringUsbDisplay = display_class(cluster.TuringUsbDisplay)
    from openpilot.tools.jetson.hud_encoder import pipeline_class
    cluster.H264UsbPipeline = pipeline_class(cluster.H264UsbPipeline, RENDER_TIMING)
    # Orin Nano uses the existing software encoder; no AGNOS/TICI encoder port.
    sys.argv = [__file__, '--input', 'live', '--output', 'usb', '--fps', '10',
                '--usb-codec', 'h264', '--usb-h264-backend', 'ffmpeg',
                '--usb-h264-ffmpeg-encoder', 'libx264', *sys.argv[1:]]
    if native:
      if os.environ.get('NEXO_PANEL_OWNER') == '1':
        from openpilot.tools.jetson.display_owner import panel_owner
      else:
        # Legacy/non-protected installs have not provisioned the RAM override.
        from display_owner import panel_owner
      def run():
        return run_owned_session(lambda: cluster.main(exit_on_error=False), panel_owner)
    else:
      run = lambda: cluster.main(exit_on_error=False)
    run()


def main():
  child = '--renderer-child' in sys.argv
  if child:
    hud_events.record('process_start')
  try:
    return _main()
  except (Exception, SystemExit) as error:
    if isinstance(error, SystemExit) and error.code in (None, 0):
      raise
    reason = failure_reason(error)
    stage = hud_events._stage or 'service_start'
    if reason in ('snapshot_expired', 'render_expired'):
      stage = 'snapshot' if reason == 'snapshot_expired' else 'render'
    elif reason == 'usb_write_failed':
      stage = 'panel_write'
    hud_events.record(stage, error, reason, service=not child)
    try:
      atomic_json(RUNTIME / 'hud-exit.json', {'updated': time.monotonic(), 'pid': os.getpid(),
                  'reason': reason, 'stage': stage, 'error_time_unix': time.time(),
                  'error_type': type(error).__name__})
    except OSError:
      pass
    if reason in ('snapshot_expired', 'render_expired', 'panel_busy'):
      raise SystemExit(76 if reason == 'panel_busy' else 75) from error
    if reason == 'hud_disabled':
      raise SystemExit(0) from error
    raise
  finally:
    if child:
      try:
        (RUNTIME / 'hud-status.json').unlink(missing_ok=True)
      except OSError:
        pass


if __name__ == '__main__':
  main()
