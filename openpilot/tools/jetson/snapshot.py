"""Read-only NEXO HUD snapshots. Never publishes vehicle messages."""
import base64
import json
import threading
import time

from openpilot.tools.jetson.state import SNAPSHOT, finite, read_fresh

MAX_SNAPSHOT = 512 * 1024
# The Comma is the wall-clock source for the Jetson HUD. The Jetson can boot
# with an unset RTC and must never substitute its own 00:xx:xx for local time.
MIN_TRUSTED_WALL_EPOCH = 1767225600  # 2026-01-01 00:00 UTC
MAX_TRUSTED_WALL_EPOCH = 4102444800  # 2100-01-01 00:00 UTC
SERVICES = (
  'carState', 'carParams', 'modelV2', 'radarState', 'liveTracks', 'longitudinalPlan',
  'lateralPlan', 'controlsState', 'selfdriveState', 'carControl', 'carOutput', 'deviceState',
  'roadCameraState', 'cameraOdometry', 'liveCalibration', 'livePose', 'drivingModelData',
  'liveDelay', 'liveParameters', 'liveTorqueParameters', 'navInstruction', 'navInstructionCarrot',
  'navRoute', 'carrotMan', 'carrotNavi', 'wideRoadCameraState',
)
# These are settings consumed by the existing renderer, not remotely writable keys.
PARAMS = (
  'ClusterHud', 'ClusterHudDebug', 'ClusterHudBrightness', 'ClusterHudOrientation',
  'ClusterHudMirror', 'ClusterHudTheme', 'ClusterHudRadarInfo', 'ClusterHudRadarDisplay',
  'ClusterHudRadarSourceColor', 'ClusterHudCameraViewMode', 'ClusterHudPanelLayout',
  'ClusterHudScreenMode', 'ClusterHudSideCamera', 'ClusterHudSideCameraTrigger',
  'ClusterHudSideCameraPreview', 'ClusterHudSideCameraLeftX', 'ClusterHudSideCameraRightX',
  'ClusterHudSideCameraY', 'ClusterHudSideCameraZoom', 'ClusterHudSideCameraWidth',
  'ClusterHudSideCameraHeight', 'ShowPlotMode', 'LanguageSetting', 'IsMetric', 'ShowDateTime',
  'IsOnroad', 'CarParams', 'CalibrationParams', 'CustomSR', 'SteerActuatorDelay',
  'LiveDelay', 'LiveParameters', 'LiveParametersV2', 'LiveTorqueParameters',
  'ClusterHudLiveFps',
)
MEMORY_PARAMS = ('CarrotNaviDebug', 'CarrotNaviImage', 'NetworkAddress')


def encode(value):
  if isinstance(value, bool):
    value = b'1' if value else b'0'
  elif isinstance(value, (dict, list)):
    value = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
  return base64.b64encode(value if isinstance(value, bytes) else str(value).encode()).decode()


# Carrot's read-only Jetson HUD reader coalesces frequent identical reads for
# 20 ms. The NEXO HUD renderer reads this large JSON repeatedly (SubMaster,
# panel chunks, params and overlays). Coalesce only in the isolated Jetson
# process to avoid redundant disk/JSON work that can delay the 10 FPS encoder.
# Unlike the upstream cache, recheck the 500 ms lease on EVERY cache hit.
HUD_READ_CACHE_S = .02
HUD_INPUT_LEASE_S = .5


class CachedSnapshotReader:
  def __init__(self):
    self.lock = threading.Lock()
    self.value = None
    self.next_read = 0.
    self.last_read = None

  def read(self):
    now = time.monotonic()
    with self.lock:
      if self.last_read is None or now < self.last_read or now >= self.next_read:
        value = read_fresh(SNAPSHOT, HUD_INPUT_LEASE_S, now=now, limit=MAX_SNAPSHOT + 1024)
        try:
          value = validate_snapshot(value) if value is not None else None
        except ValueError:
          value = None
        self.value = value
        self.last_read = now
        self.next_read = now + HUD_READ_CACHE_S
      value = self.value
    # Re-evaluate even within the cache window: an expired or future timestamp
    # must NEVER remain visible merely because a cached read is still active.
    updated = value.get('updated') if isinstance(value, dict) else None
    age = time.monotonic() - updated if finite(updated) else -1.
    return value if 0 <= age < HUD_INPUT_LEASE_S else None


_SNAPSHOT_READER = CachedSnapshotReader()


def read_snapshot():
  return _SNAPSHOT_READER.read()


def validate_snapshot(value):
  if value.get('version') != 1 or not finite(value.get('sent')) or value['sent'] < 0:
    raise ValueError('invalid HUD snapshot version/time')
  if 'wall_time_unix' in value and (not finite(value['wall_time_unix'])
                                    or not MIN_TRUSTED_WALL_EPOCH <= value['wall_time_unix'] < MAX_TRUSTED_WALL_EPOCH):
    raise ValueError('invalid HUD snapshot wall clock')
  for key in ('events', 'params', 'mono', 'received', 'valid', 'alive', 'cameras'):
    if not isinstance(value.get(key), dict):
      raise ValueError(f'invalid HUD snapshot {key}')
  if set(value['events']) - set(SERVICES + ('can',)) or set(value['params']) - set(PARAMS + MEMORY_PARAMS):
    raise ValueError('non-display data in HUD snapshot')
  for key in ('mono', 'received'):
    if not all(finite(v) and v >= 0 for v in value[key].values()):
      raise ValueError('invalid HUD message timestamp')
  for key in ('params', 'events'):
    if not all(isinstance(v, str) for v in value[key].values()):
      raise ValueError('invalid HUD data encoding')
  if 'signal_display' in value:
    from openpilot.tools.jetson.hud_signals import sanitize
    value['signal_display'] = sanitize(value['signal_display'], value['sent'], value['cameras'])
  return value


def parking_frames(frames):
  return [f for f in frames if f.src == 0 and f.address == 0x4F4 and len(f.dat) == 8]


class SnapshotBuilder:
  def __init__(self):
    from openpilot.cereal import messaging
    from openpilot.common.params import Params
    self.messaging = messaging
    self.sm = messaging.SubMaster(list(SERVICES))
    self.can = messaging.sub_sock('can', conflate=False)
    self.params = Params()
    self.memory = Params('/dev/shm/params')
    self.cached = {}
    self.settings = {}
    self.next_params = 0.
    self.parking = None

  def packet(self, cameras=None):
    from openpilot.cereal import log
    now = time.monotonic()
    wall_time = time.time()
    self.sm.update(0)
    for name in SERVICES:
      if self.sm.updated[name]:
        event = log.Event.new_message(logMonoTime=self.sm.logMonoTime[name], valid=self.sm.valid[name])
        setattr(event, name, self.sm[name])
        self.cached[name] = encode(event.to_bytes())
    # A separate bounded RX subscription is required: conflating entire CAN
    # batches loses SPAS12 when another batch arrives between HUD ticks.
    for _ in range(64):
      event = self.messaging.recv_one_or_none(self.can)
      if event is None:
        break
      frames = parking_frames(event.can)
      if frames:
        filtered = log.Event.new_message(logMonoTime=event.logMonoTime, valid=event.valid)
        filtered.init('can', len(frames))
        for i, frame in enumerate(frames):
          filtered.can[i] = frame
        self.parking = filtered
    if now >= self.next_params:
      self.settings = {}
      for store, keys in ((self.params, PARAMS), (self.memory, MEMORY_PARAMS)):
        for key in keys:
          value = store.get(key)
          if value is not None:
            self.settings[key] = encode(value)
      self.next_params = now + 1
    value = {'version': 1, 'sent': now, 'events': dict(self.cached), 'params': self.settings,
             'received': dict(self.sm.recv_time), 'mono': dict(self.sm.logMonoTime),
             'valid': dict(self.sm.valid), 'alive': dict(self.sm.alive), 'cameras': cameras or {}}
    # Display-only trusted UTC epoch. Preserve the existing cross-device
    # monotonic translation for all vehicle and navigation messages.
    if finite(wall_time) and MIN_TRUSTED_WALL_EPOCH <= wall_time < MAX_TRUSTED_WALL_EPOCH:
      value['wall_time_unix'] = wall_time
    from openpilot.tools.jetson.hud_observations import signal_snapshot
    value['signal_display'] = signal_snapshot(now, value['cameras'])
    if self.parking is not None:
      value['events']['can'] = encode(self.parking.to_bytes())
      value['mono']['can'] = self.parking.logMonoTime
      value['received']['can'] = self.parking.logMonoTime / 1e9
      value['valid']['can'] = self.parking.valid
      value['alive']['can'] = 0 <= now - self.parking.logMonoTime / 1e9 < 1.5
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT and 'signal_display' in value:
      value.pop('signal_display')
      raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT:
      raise ValueError('HUD snapshot exceeds budget; display packet skipped')
    return raw
