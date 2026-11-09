"""Optical evidence on one forward ROAD frame. No radar, model or CAN output."""
from collections import deque

from openpilot.tools.jetson.state import finite

# Ages are measured from capture, never from completion of optional processing.
LEASE = .35


def road_targets(sm, camera):
  """Project only real, timestamp-aligned radar leads using the C4 road calibration."""
  if not camera or not finite(camera.get('time')):
    return []
  stamp = camera['time']
  def fresh(name, limit):
    t = sm.logMonoTime[name] * 1e-9
    return sm.valid[name] and sm.alive[name] and t > 0 and abs(t - stamp) < limit
  if not all(fresh(name, limit) for name, limit in [('radarState', .12), ('roadCameraState', .15),
                                                     ('liveCalibration', 1.5), ('deviceState', 2.)]):
    return []
  from openpilot.common.transformations.camera import DEVICE_CAMERAS, get_view_frame_from_road_frame
  cal = sm['liveCalibration']
  sensor = str(sm['roadCameraState'].sensor).lower()
  device = DEVICE_CAMERAS.get((str(sm['deviceState'].deviceType).lower(), sensor))
  if not device or device.fcam.size != (camera.get('source_width'), camera.get('source_height')):
    return []
  if str(cal.calStatus) != 'calibrated' or len(cal.rpyCalib) != 3 or not len(cal.height) or not .5 < cal.height[0] < 3:
    return []
  projection = device.fcam.intrinsics @ get_view_frame_from_road_frame(*cal.rpyCalib, cal.height[0])
  targets = []
  radar = sm['radarState']
  for lead in (radar.leadOne, radar.leadTwo):
    # dRel in radarState is in the radar frame; the camera is behind the front radar.
    ident, distance, lateral = lead.radarTrackId, lead.dRel, lead.yRel
    if not (lead.status and lead.radar and ident >= 0 and 3 < distance < 40 and abs(lateral) < 8):
      continue
    corners = []
    for y, z in ((lateral + .95, 1.6), (lateral - .95, 1.6), (lateral + .95, .25), (lateral - .95, .25)):
      u, v, depth = projection @ (distance + 1.52, y, z, 1.)
      if depth <= 0:
        break
      corners.append((u / depth / device.fcam.width, v / depth / device.fcam.height))
    if len(corners) != 4:
      continue
    xs, ys = zip(*corners)
    box = [min(xs), min(ys), max(xs), max(ys)]
    if all(0 < x < 1 for x in box):
      targets.append(dict(id=ident, box=box, d=round(distance, 2), y=round(lateral, 2)))
  return targets


def overlap(a, b):
  return max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))


def optical(crop):
  """Measure compact red and amber emitters against their local background, in RGB."""
  import numpy as np
  if len(crop.shape) != 3 or crop.shape[2] != 3 or crop.shape[0] < 20 or crop.shape[1] < 32:
    return None
  h, w = crop.shape[:2]
  def lamp(x1, x2, y1, y2, amber):
    patch = crop[int(h*y1):int(h*y2), int(w*x1):int(w*x2)].astype(np.int16)
    red, green, blue = patch.transpose(2, 0, 1)
    mask = ((red >= 145) & (red - blue >= 65) &
            ((green >= 85) & (red > green * 1.08) & (green - blue >= 45) if amber else (red - green >= 65)))
    n = int(mask.sum())
    if not 2 <= n <= .33 * mask.size:
      return False
    yy, xx = np.nonzero(mask)
    return bool(n / ((np.ptp(xx) + 1) * (np.ptp(yy) + 1)) >= .38 and
                np.median(red[mask]) > np.median(red[~mask]) + 55)
  left, right = (.04, .35), (.65, .96)
  tails = all(lamp(*zone, .28, .80, False) for zone in (left, right))
  center = lamp(.34, .66, .03, .48, False)
  return tails, center, lamp(*left, .25, .80, True), lamp(*right, .25, .80, True)


class Track:
  def __init__(self):
    self.samples = deque(maxlen=40)
    self.armed = False

  def observe(self, stamp, value):
    if self.samples and not 0 < stamp - self.samples[-1][0] <= .32:
      self.samples.clear()
      self.armed = False
    tails, center, left, right = value
    self.samples.append((stamp, tails, center, left, right))
    if tails and not center:
      self.armed = True
    if not tails:
      return 0
    # Require a seen off state, both red tail regions and two lit center samples.
    brake = self.armed and center and len(self.samples) >= 2 and all(self.samples[-2][1:3])
    def flashes(index):
      edges = [(b[0], b[index]) for a, b in zip(self.samples, list(self.samples)[1:]) if a[index] != b[index]]
      if len(edges) < 3 or [on for _, on in edges[-3:]] != [True, False, True]:
        return None
      a, b, c = (t for t, _ in edges[-3:])
      return (a, b, c) if .15 <= b-a <= .95 and .15 <= c-b <= .95 and stamp-c <= .85 else None
    le, ri = flashes(3) if left else None, flashes(4) if right else None
    mask = int(brake)
    if le and ri and all(abs(a-b) <= .12 for a, b in zip(le, ri)):
      return mask | 8
    if le and not any(sample[4] for sample in self.samples if stamp - sample[0] < 1.2):
      return mask | 2
    if ri and not any(sample[3] for sample in self.samples if stamp - sample[0] < 1.2):
      return mask | 4
    return mask


def allowed(mask, prefs):
  if not prefs.get('enabled'):
    return 0
  enabled_mask = (1 if prefs.get('brake') else 0) | (6 if prefs.get('turn') else 0) | (8 if prefs.get('hazard') else 0)
  return mask & enabled_mask
