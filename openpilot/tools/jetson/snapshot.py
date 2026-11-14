"""Read-only NEXO HUD snapshots. Never publishes vehicle messages."""
import base64
from functools import lru_cache
import json
import logging
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
  'ClusterHudVehicleStyle', 'ClusterHudRadarSourceColor', 'ClusterHudCameraViewMode', 'ClusterHudPanelLayout',
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
    with self.lock:
      now = time.monotonic()
      if self.last_read is None or now < self.last_read or now >= self.next_read:
        value = read_fresh(SNAPSHOT, HUD_INPUT_LEASE_S, limit=MAX_SNAPSHOT + 1024)
        try:
          value = validate_snapshot(value) if value is not None else None
        except ValueError:
          value = None
        self.value = value
        self.last_read = now
        self.next_read = now + HUD_READ_CACHE_S
        if value and finite(value.get('updated')):
          self.next_read = min(self.next_read, value['updated'] + HUD_INPUT_LEASE_S)
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
  if 'jetlink_inference_active' in value and type(value['jetlink_inference_active']) is not bool:
    # This optional HUD-only indicator must not invalidate a driving frame.
    value.pop('jetlink_inference_active')
  if 'jetlink_ai' in value:
    from openpilot.tools.jetson.jetlink_ai_indicator import sanitize
    status = sanitize(value['jetlink_ai'])
    if status is None:
      value.pop('jetlink_ai')
    else:
      value['jetlink_ai'] = status
  if 'signal_display' in value:
    from openpilot.tools.jetson.hud_signals import sanitize
    value['signal_display'] = sanitize(value['signal_display'], value['sent'], value['cameras'])
  if 'gear_view' in value:
    view = value['gear_view']
    if (not isinstance(view, dict) or type(view.get('sample_ns')) is not int or not 0 <= view['sample_ns'] < 2**63
        or not isinstance(view.get('gear'), str)
        or len(view['gear']) > 20 or any(type(view.get(k)) is not bool for k in ('left', 'right', 'can_ok', 'park_enabled'))):
      value.pop('gear_view')  # Optional metadata cannot invalidate a driving frame.
  return value


@lru_cache(maxsize=1)
def ems20_decoder():
  # Load only in the Comma snapshot producer. The packaged Jetson renderer
  # has neither the vehicle's DBC nor its native Params extension.
  try:
    from opendbc.can.dbc import DBC
    from opendbc.can.parser import CANDefine, get_raw_value
    dbc = 'hyundai_kia_generic'
    signal = 'HYDROGEN_GEAR_SHIFTER'
    definition = DBC(dbc).name_to_msg['EMS20'].sigs[signal]
    labels = CANDefine(dbc).dv['EMS20'][signal]
    return lambda data: labels.get(get_raw_value(data, definition))
  except Exception:
    logging.getLogger(__name__).exception('NEXO HUD gear validation unavailable')
    return None


def current_nexo(car_params):
  if car_params:
    from openpilot.cereal import car
    try:
      with car.CarParams.from_bytes(car_params) as params:
        return params.carFingerprint == 'HYUNDAI_NEXO_1ST_GEN'
    except Exception:
      pass
  return False


def verified_hud_gear(gear, car_state_fresh, raw_gear=None):
  """Prefer fresh, valid vehicle carState; a fresh conflicting EMS20 vetoes it.

  An optional raw CAN subscription can miss frames even when the vehicle
  parser remains healthy. Never use the last retained raw code as the HUD
  sample timestamp, and never replace an actual conflict with stale P/R.
  """
  if not car_state_fresh or gear not in ('park', 'reverse', 'neutral', 'drive'):
    return 'unknown'
  if raw_gear is not None and raw_gear != gear[0].upper():
    return 'unknown'
  return gear


def display_can_frames(frames):
  parking, gear_data = [], None
  for frame in frames:
    if frame.src == 0:
      address = frame.address
      if address == 0x4F4 and len(frame.dat) == 8:
        parking.append(frame)
      elif address == 0x200:
        gear_data = bytes(frame.dat)
  return parking, gear_data


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
    self.gear_message_ns = 0
    self.gear_data = b''

  wire_budget = None

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
    filtered = None
    for _ in range(64):
      event = self.messaging.recv_one_or_none(self.can)
      if event is None:
        break
      # The Hyundai decoder retains its last gear. Require the physical
      # EMS20 frame on the existing receive-only subscription as corroboration.
      frames, gear_data = display_can_frames(event.can)
      if gear_data is not None:
        self.gear_message_ns = event.logMonoTime if event.valid and 6 <= len(gear_data) <= 8 else 0
        self.gear_data = gear_data
      if frames:
        filtered = log.Event.new_message(logMonoTime=event.logMonoTime, valid=event.valid)
        filtered.init('can', len(frames))
        for i, frame in enumerate(frames):
          filtered.can[i] = frame
    if filtered is not None:
      self.parking = (encode(filtered.to_bytes()), filtered.logMonoTime, filtered.valid)
    if now >= self.next_params:
      self.settings = {}
      for store, keys in ((self.params, PARAMS), (self.memory, MEMORY_PARAMS)):
        for key in keys:
          value = store.get(key)
          if key == 'CarParams':
            # This key is cleared on each onroad transition. It is available
            # before the next (50-second) carParams broadcast on a late join.
            self.nexo_vehicle = not self.sm.valid['carParams'] and current_nexo(value)
          if value is not None:
            encoded = encode(value)
            # Omit settings the bounded NCM packetizer would always remove.
            # Avoid serializing the same large calibration/image at every tick.
            if self.wire_budget and ((key == 'LiveTorqueParameters' and len(encoded) >= self.wire_budget)
                                     or (key == 'CarrotNaviImage' and len(encoded) > 16 * 1024)):
              continue
            self.settings[key] = encoded
      self.next_params = now + 1
      # Older Jetsons reject unknown keys inside 'params'. An optional view
      # record lets the Comma and Jetson be updated in either order.
      park_pref = self.params.get('ClusterHudParkVehicle')
      self.park_vehicle = park_pref in (None, 1, b'1', '1')
      from openpilot.tools.jetson import lamp_settings
      self.vehicle_lamps = lamp_settings.read()
      self.nav_lamp = None
      from openpilot.selfdrive.carrot.cluster.signal_display import nav_phase
      raw_lamp = self.memory.get('TrafficLight')
      if raw_lamp and len(raw_lamp) < 2048 and nav_phase(raw_lamp, now):
        lamp = json.loads(raw_lamp)
        self.nav_lamp = {k: lamp[k] for k in ('lamp', 'ts', 'remain')}
    value = {'version': 1, 'sent': now, 'events': dict(self.cached), 'params': self.settings,
             'received': dict(self.sm.recv_time), 'mono': dict(self.sm.logMonoTime),
             'valid': dict(self.sm.valid), 'alive': dict(self.sm.alive), 'cameras': cameras or {}}
    # A fresh successful Jetson model inference is different from a connected
    # HUD/USB link or an enabled switch. Never guess the inference state.
    from openpilot.tools.jetson.jetlink_ai_indicator import status_on_comma
    value['jetlink_ai'] = status_on_comma(now)
    value['jetlink_inference_active'] = value['jetlink_ai']['inference_active']
    nexo = (self.sm['carParams'].carFingerprint == 'HYUNDAI_NEXO_1ST_GEN'
            if self.sm.valid['carParams'] else self.nexo_vehicle)
    if nexo:
      cs = self.sm['carState']
      gear = str(cs.gearShifter)
      decode = ems20_decoder()
      # carState is the authoritative, frequently updated vehicle gear.
      # EMS20 corroboration is opportunistic: the separate CAN subscription
      # sometimes misses it for seconds despite fresh carState data. A recent
      # *conflicting* raw value still rejects the scene without using old P/R.
      car_stamp = self.sm.logMonoTime['carState']
      car_fresh = (self.sm.valid['carState'] and self.sm.alive['carState']
                   and car_stamp > 0 and 0 <= now - car_stamp * 1e-9 < .5
                   and cs.canValid and not cs.canTimeout)
      raw_recent = (self.gear_message_ns > 0
                    and 0 <= now - self.gear_message_ns * 1e-9 < .5)
      # Unknown/reserved *recent* raw codes must veto the retained enum.
      # None is reserved for a genuinely missing corroborating frame.
      raw_gear = (decode(self.gear_data) or 'invalid') if raw_recent and decode else None
      confirmed = verified_hud_gear(gear, car_fresh, raw_gear)
      value['gear_view'] = {
        'park_enabled': self.park_vehicle, 'gear': confirmed,
        'sample_ns': car_stamp,
        'can_ok': bool(cs.canValid and not cs.canTimeout),
        'left': bool(cs.leftBlinker), 'right': bool(cs.rightBlinker),
      }
    if getattr(self, 'vehicle_lamps', {}).get('enabled'):
      value['vehicle_lamps'] = self.vehicle_lamps
      if cameras and cameras.get('road'):
        from openpilot.tools.jetson.vehicle_lamps import road_targets
        try:
          value['lamp_targets'] = road_targets(self.sm, cameras['road'])
        except (ImportError, AttributeError, KeyError, ValueError, ArithmeticError, TypeError):
          pass  # Optional display metadata cannot suppress a vehicle snapshot.
    if getattr(self, 'nav_lamp', None):
      value['nav_lamp'] = self.nav_lamp
    # Display-only trusted UTC epoch. Preserve the existing cross-device
    # monotonic translation for all vehicle and navigation messages.
    if finite(wall_time) and MIN_TRUSTED_WALL_EPOCH <= wall_time < MAX_TRUSTED_WALL_EPOCH:
      value['wall_time_unix'] = wall_time
    from openpilot.tools.jetson.hud_observations import signal_snapshot
    value['signal_display'] = signal_snapshot(now, value['cameras'])
    if self.parking is not None:
      payload, stamp, valid = self.parking
      value['events']['can'] = payload
      value['mono']['can'] = stamp
      value['received']['can'] = stamp / 1e9
      value['valid']['can'] = valid
      value['alive']['can'] = 0 <= now - stamp / 1e9 < 1.5
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT:
      # The NCM packetizer already omits these optional blobs under pressure.
      # Reaching the producer limit first used to lose the entire vehicle tick.
      params = dict(value['params'])
      params.pop('LiveTorqueParameters', None)
      if len(params.get('CarrotNaviImage', '')) > 16 * 1024:
        params.pop('CarrotNaviImage', None)
      value['params'] = params
      raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT and ('vehicle_lamps' in value or 'nav_lamp' in value):
      for key in ('vehicle_lamps', 'lamp_targets', 'nav_lamp'):
        value.pop(key, None)
      raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT and 'signal_display' in value:
      value.pop('signal_display')
      raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT:
      value.pop('gear_view', None)
      raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT:
      raise ValueError('HUD snapshot exceeds budget; display packet skipped')
    return raw
