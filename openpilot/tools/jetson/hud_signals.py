"""Bounded, display-only ROAD observations. No detector or vehicle-control writes."""
import json

from openpilot.tools.jetson.state import finite

MAX_AGE = .35
MAX_WIRE = 4096
SIZE = [1344, 760]


def clean_record(value, now):
  if not isinstance(value, dict) or value.get('version') != 1 or value.get('stream') != 'road':
    return None
  stamp, fid, session = value.get('timestamp'), value.get('frame_id'), value.get('session')
  if not finite(now) or not finite(stamp) or not 0 < stamp <= now or now - stamp >= MAX_AGE:
    return None
  if value.get('size') != SIZE or type(fid) is not int or not 0 <= fid <= 0xffffffff:
    return None
  if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isascii():
    return None
  if not isinstance(value.get('tracks'), list) or len(value['tracks']) > 20:
    return None
  tracks = []
  for track in value['tracks']:
    if not isinstance(track, dict) or not finite(track.get('age')) or track['age'] != 0:
      continue
    box, ident = track.get('box'), track.get('id')
    if type(ident) is not int or not 0 < ident <= 1000000:
      continue
    if not isinstance(box, list) or len(box) != 4 or not all(finite(v) for v in box):
      continue
    x1, y1, x2, y2 = box
    if not 0 <= x1 < x2 <= SIZE[0] or not 0 <= y1 < y2 <= SIZE[1]:
      continue
    evidence = track.get('evidence')
    raw = evidence.get('raw') if isinstance(evidence, dict) else track.get('raw')
    # Current optical evidence wins over the tracker's previously confirmed state.
    # This detector has no yellow class; never infer it from a transition.
    raw = raw if raw in ('red', 'green') else 'unknown'
    tracks.append({'id': ident, 'box': box, 'age': 0, 'raw': raw})
  record = dict(version=1, stream='road', size=SIZE, timestamp=stamp, frame_id=fid, session=session, tracks=tracks)
  return record if len(json.dumps(record, allow_nan=False, separators=(',', ':')).encode()) <= MAX_WIRE else None


def matching_camera(record, cameras):
  camera = cameras.get('road') if isinstance(cameras, dict) else None
  return bool(isinstance(camera, dict) and type(camera.get('frame')) is int
              and camera['frame'] == record['frame_id'] and finite(camera.get('time'))
              and abs(camera['time'] - record['timestamp']) < .000001
              and [camera.get('source_width'), camera.get('source_height')] == SIZE
              and isinstance(camera.get('jpeg'), str) and 0 < len(camera['jpeg']) <= 44 * 1024)


def sanitize(value, sent, cameras):
  try:
    record = clean_record(value.get('record'), sent) if isinstance(value, dict) and value.get('version') == 2 else None
    return {'version': 2, 'record': record if record is not None and matching_camera(record, cameras) else None}
  except (ValueError, TypeError, OverflowError, RecursionError):
    return {'version': 2, 'record': None}


def items(record):
  return [{'kind': track['raw'], 'x': (track['box'][0] + track['box'][2]) / (2 * SIZE[0]),
           'y': (track['box'][1] + track['box'][3]) / (2 * SIZE[1])} for track in record['tracks']]


class RemoteSignals:
  """Map sender age onto receiver time without refreshing duplicate observations."""
  def __init__(self):
    self.epoch = None
    self.retired = []
    self.high = None
    self.content = None
    self.deadline = 0.
    self.blocked = False

  def read(self, snapshot, now):
    if not isinstance(snapshot, dict) or not finite(now) or not finite(snapshot.get('updated')):
      self.blocked = True
      return []
    received, sent, epoch = snapshot['updated'], snapshot.get('sent'), snapshot.get('session')
    if not isinstance(epoch, str) or len(epoch) != 32 or not finite(sent) or not 0 <= now - received < MAX_AGE:
      self.blocked = True
      return []
    record = sanitize(snapshot.get('signal_display'), sent, snapshot.get('cameras'))['record']
    if record is None:
      self.blocked = True
      return []
    key = (record['session'], record['timestamp'], record['frame_id'])
    content = tuple((track['id'], tuple(track['box']), track['raw']) for track in record['tracks'])
    deadline = received + MAX_AGE - (sent - record['timestamp'])
    changed = epoch != self.epoch
    if changed:
      if epoch in self.retired:
        self.blocked = True
        return []
      if self.epoch is not None:
        self.retired = (self.retired + [self.epoch])[-8:]
      self.epoch = epoch
    if key == self.high:
      self.deadline = min(self.deadline, deadline)
      if content != self.content:
        self.blocked = True
    else:
      # Camera/observer restart may reset frame IDs, but capture time must advance.
      if not changed and self.high is not None and (key[1] <= self.high[1] or (key[0] == self.high[0] and key[2] <= self.high[2])):
        self.blocked = True
        return []
      self.high, self.deadline, self.blocked = key, deadline, False
      self.content = content
    if self.blocked or now >= self.deadline:
      return []
    return items(record)
