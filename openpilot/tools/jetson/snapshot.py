"""Read-only NEXO HUD snapshots. Never publishes vehicle messages."""
import base64
import json
import time

from openpilot.tools.jetson.state import SNAPSHOT, finite, read_fresh

MAX_SNAPSHOT = 512 * 1024
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


def read_snapshot():
  value = read_fresh(SNAPSHOT, .5, limit=MAX_SNAPSHOT + 1024)
  try:
    return validate_snapshot(value) if value is not None else None
  except ValueError:
    return None


def validate_snapshot(value):
  if value.get('version') != 1 or not finite(value.get('sent')) or value['sent'] < 0:
    raise ValueError('invalid HUD snapshot version/time')
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
    if self.parking is not None:
      value['events']['can'] = encode(self.parking.to_bytes())
      value['mono']['can'] = self.parking.logMonoTime
      value['received']['can'] = self.parking.logMonoTime / 1e9
      value['valid']['can'] = self.parking.valid
      value['alive']['can'] = 0 <= now - self.parking.logMonoTime / 1e9 < 1.5
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT:
      raise ValueError('HUD snapshot exceeds budget; display packet skipped')
    return raw
