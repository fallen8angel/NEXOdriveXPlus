"""Optional low-priority Jetson worker. Consume the latest existing preview; never queue frames."""
import base64
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import time

from openpilot.tools.jetson import lamp_settings
from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite, read_fresh
from openpilot.tools.jetson.vehicle_lamps import LEASE, Track, allowed, optical, overlap

OUTPUT = RUNTIME / 'vehicle-lamps.json'
# Optional locally provisioned rear-crop ONNX classifier: RGB NCHW float32
# 1x3x64x96 in [0,1]; four independent probabilities (rear, brake, left, right).
MODEL = Path('/opt/carrot-jetlink/models/rear-lamps.onnx')


def enabled(snapshot):
  prefs = (snapshot or {}).get('vehicle_lamps')
  return lamp_settings.valid(prefs) and prefs['enabled'] and any(prefs[k] for k in ('brake', 'turn', 'hazard'))


class Worker:
  def __init__(self):
    self.child = None
    self.retry = 0.
    self.failures = 0

  def tick(self, snapshot):
    if self.child and self.child.poll() is not None:
      if self.child.returncode:
        self.failures += 1
        self.retry = time.monotonic() + 30
      self.child = None
    if snapshot is None:
      return  # The worker expires evidence and idles across a short input gap.
    if not enabled(snapshot):
      self.failures = 0
      self.retry = 0.
      if self.child:
        self.close()
    elif self.failures < 3 and not self.child and time.monotonic() >= self.retry:
      try:
        self.child = subprocess.Popen([sys.executable, '-m', 'openpilot.tools.jetson.lamp_worker', str(os.getpid())],
                                      stdin=subprocess.DEVNULL)
      except OSError:
        self.failures += 1
        self.retry = time.monotonic() + 30

  def close(self):
    from openpilot.tools.jetson.process import stop_process
    if stop_process(self.child, 'vehicle-lamps'):
      self.child = None


class Analyzer:
  def __init__(self, model=None):
    self.tracks = {}
    self.session = None
    self.frame = -1
    self.stamp = -1.
    self.model = model

  def analyze(self, snap, yolo, now):
    import numpy as np
    from PIL import Image
    cam = snap['cameras'].get('road', {})
    stamp, fid = cam.get('time'), cam.get('frame')
    session = snap['session']
    if session != self.session:
      self.tracks.clear()
      self.frame = -1
      self.stamp = -1.
      self.session = session
    if not finite(stamp) or type(fid) is not int or fid < 0 or stamp <= self.stamp:
      return None
    if fid <= self.frame:
      self.tracks.clear()  # Newer exposure after a ROAD camera frame-counter reset.
    self.frame = fid
    self.stamp = stamp
    captured = snap['updated'] - (snap['sent'] - stamp)
    rows = []
    report = dict(updated=captured, session=session, frame=fid, road_time=stamp, tracks=rows)
    if not enabled(snap) or stamp > snap['sent'] or not 0 <= now - captured < LEASE:
      self.tracks.clear()
      return report
    targets = snap.get('lamp_targets', [])
    if not isinstance(targets, list) or not 0 < len(targets) <= 2:
      self.tracks.clear()
      return report
    with Image.open(BytesIO(base64.b64decode(cam['jpeg'], validate=True))) as image:
      if image.size != (cam['width'], cam['height']) or image.width > 384 or image.height > 384:
        self.tracks.clear()
        return report
      rgb = np.asarray(image.convert('RGB'))
    h, w = rgb.shape[:2]
    # Use a YOLO box only on the exact original ROAD exposure. Encoder IDs
    # and the Jetson's receipt clock do not identify a camera exposure.
    synchronized = bool(yolo and yolo.get('vehicle_detector') is True and type(yolo.get('road_capture_ns')) is int and
                        abs(yolo['road_capture_ns'] * 1e-9 - stamp) < .000001)
    boxes = []
    if synchronized:
      for o in yolo['objects']:
        if o['name'] in ('car', 'truck', 'bus') and o['conf'] >= .6:
          boxes.append([o[k] / yolo[dimension] for k, dimension in
                        (('x1','width'), ('y1','height'), ('x2','width'), ('y2','height'))])
    active = {}
    for target in targets:
      box, ident = target['box'], target['id']
      if any(other is not target and (other['id'] == ident or overlap(box, other['box']) > 0) for other in targets):
        continue
      if synchronized:
        matches = [b for b in boxes if overlap(box, b) > .5 * max((b[2]-b[0])*(b[3]-b[1]), (box[2]-box[0])*(box[3]-box[1]))]
        if len(matches) != 1:
          continue
        box = matches[0]
        if any(other is not target and overlap(box, other['box']) > 0 for other in targets):
          continue
      crop = rgb[int(box[1]*h):int(box[3]*h), int(box[0]*w):int(box[2]*w)]
      value = optical(crop)
      if value is None:
        continue
      if self.model is not None:
        import cv2
        self.model.setInput(cv2.dnn.blobFromImage(crop, 1/255., (96, 64)))
        scores = self.model.forward().reshape(-1)
        if scores.shape != (4,) or not np.isfinite(scores).all() or not ((0 <= scores) & (scores <= 1)).all():
          raise ValueError('invalid rear-lamp classifier output')
        if scores[0] < .9:
          continue
        value = (value[0], *(v and p >= .9 for v, p in zip(value[1:], scores[1:])))
      previous = self.tracks.get(ident)
      track = previous[0] if previous and abs(previous[1]-target['d']) < 2 and abs(previous[2]-target['y']) < .5 else Track()
      active[ident] = (track, target['d'], target['y'])
      mask = allowed(track.observe(stamp, value), snap['vehicle_lamps'])
      if mask:
        rows.append(dict(id=ident, mask=mask, d=target['d'], y=target['y']))
    self.tracks = active
    return report


def run(parent):
  os.nice(10)
  os.environ.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
  from openpilot.tools.jetson.snapshot import read_snapshot
  from openpilot.tools.jetson.yolo import read_objects
  model = None
  if MODEL.is_file():
    import cv2
    cv2.setNumThreads(1)
    model = cv2.dnn.readNetFromONNX(str(MODEL))
  analyzer = Analyzer(model)
  last_input = time.monotonic()
  while os.getppid() == parent:
    snap = read_snapshot()
    if snap is None and time.monotonic() - last_input < 2:
      time.sleep(.1)
      continue
    if not enabled(snap):
      return
    last_input = time.monotonic()
    report = analyzer.analyze(snap, read_objects(), time.monotonic())
    if report is not None:
      atomic_json(OUTPUT, report)
    time.sleep(.08)


def visible(snapshot, now):
  if not enabled(snapshot):
    return ()
  report = read_fresh(OUTPUT, LEASE, now, limit=4096)
  camera = snapshot.get('cameras', {}).get('road')
  if (not isinstance(camera, dict) or not report or report.get('session') != snapshot['session']
      or type(report.get('frame')) is not int or report['frame'] != camera.get('frame')
      or not finite(report.get('road_time')) or report['road_time'] != camera.get('time')):
    return ()
  rows = report.get('tracks')
  if not isinstance(rows, list) or len(rows) > 2:
    return ()
  return tuple({**row, 'mask': allowed(row['mask'], snapshot['vehicle_lamps'])} for row in rows
               if isinstance(row, dict) and type(row.get('id')) is int and type(row.get('mask')) is int
               and 0 < row['mask'] < 16 and finite(row.get('d')) and finite(row.get('y')))


if __name__ == '__main__':
  run(int(sys.argv[1]))
