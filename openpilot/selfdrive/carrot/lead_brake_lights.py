"""Opt-in C4 observation, display and logging only. No control publisher.

Detector selectively adapted from carrot-wip d34224b4. OFF creates no worker,
camera, subscription, detector import or result-file polling.
"""
import json
import math
from pathlib import Path
import time

PARAM = 'LeadBrakeLights'
PATH = Path('/dev/shm/nexo-lead-brake.json')


def requested(params):
  try:
    return params.get_bool(PARAM)
  except Exception:
    return False


def display_result(record, now, camera_frame=None):
  try:
    if (not isinstance(record, dict) or record['version'] != 1 or record['control_permission'] is not False
        or record['state'] not in ('candidate', 'unknown') or record['stream'] != 'road'
        or (record['width'], record['height']) != (1344, 760)
        or not 0 <= now-record['timestamp'] <= .25
        or type(record['frame_id']) is not int or record['frame_id'] < 0):
      return None
    if camera_frame is not None:
      fid, stamp, width, height, wide = camera_frame
      if wide or (width, height) != (1344, 760) or abs(fid-record['frame_id']) > 5 or not 0 <= now-stamp <= .25:
        return None
    box = record.get('box')
    if box is not None and (len(box) != 4 or not all(type(x) in (float, int) and math.isfinite(x) for x in box)
                            or not 0 <= box[0] < box[2] <= 1344 or not 0 <= box[1] < box[3] <= 760):
      return None
    return record
  except (KeyError, TypeError, ValueError, OverflowError):
    return None


def read_record(now=None):
  try:
    with PATH.open('rb') as source:
      raw = source.read(4097)
    return display_result(json.loads(raw), time.monotonic() if now is None else now) if len(raw) <= 4096 else None
  except (OSError, ValueError, RecursionError):
    return None


class BrakeDisplayReader:
  def __init__(self):
    self.next_params = self.next_read = 0.
    self.enabled = False
    self.record = None

  def read(self, params, camera_frame=None, now=None):
    now = time.monotonic() if now is None else now
    if now >= self.next_params:
      self.enabled = requested(params)
      self.next_params = now + .5
    if not self.enabled:
      self.record = None
      return None
    if now >= self.next_read:
      remote = getattr(params, 'lead_brake_observation', None)
      self.record = remote() if callable(remote) else read_record(now)
      self.next_read = now + .05
    return display_result(self.record, now, camera_frame)


class BrakeRuntime:
  def __init__(self):
    self.sm = self.detector = None
    self.last_frame = self.last_stamp = None
    self.next_sample = self.next_log = 0.

  def update(self, frame, client, params, now=None, logger=None):
    if not requested(params):
      self.sm = self.detector = None
      return None
    now = time.monotonic() if now is None else now
    frame_id, stamp = client.frame_id, client.timestamp_eof / 1e9
    if (frame is None or now < self.next_sample or frame_id == self.last_frame
        or self.last_stamp is not None and stamp <= self.last_stamp or not 0 <= now-stamp <= .15
        or (frame.width, frame.height) != (1344, 760) or getattr(frame, 'frame_id', None) != frame_id):
      return None
    self.last_frame, self.last_stamp = frame_id, stamp
    self.next_sample = now + .1
    from openpilot.selfdrive.modeld.signal_tracking_shadow import copy_nv12_rgb
    rgb = copy_nv12_rgb(frame)
    if (frame.frame_id != frame_id or client.frame_id != frame_id
        or client.timestamp_eof / 1e9 != stamp or time.monotonic()-stamp > .15):
      return None  # Reject VisionIPC buffer reuse during conversion.
    if self.sm is None:
      from openpilot.cereal import messaging
      from tools.signal_analysis.lead_brake_detector import LeadBrakeDetector  # noqa: TID251 - root offline observer
      self.sm = messaging.SubMaster(['radarState', 'modelV2', 'carState', 'liveCalibration'])
      self.detector = LeadBrakeDetector()
    self.sm.update(0)
    fresh = all(self.sm.seen[k] and self.sm.valid[k] and 0 <= now-self.sm.logMonoTime[k]/1e9 <= (
      2. if k == 'liveCalibration' else .25) for k in ('radarState', 'modelV2', 'carState', 'liveCalibration'))
    start = time.monotonic()
    result = dict(state='unknown', reason='stale_context', box=None)
    if fresh:
      leads = self.sm['modelV2'].leadsV3
      result = self.detector.process(rgb, stamp, self.sm['radarState'].leadOne.to_dict(),
                                     leads[0].to_dict() if leads else {}, self.sm['carState'].to_dict(),
                                     self.sm['liveCalibration'].to_dict())
    else:
      self.detector.reset()
    work_ms = (time.monotonic()-start)*1000
    if work_ms > 5:
      result = dict(state='unknown', reason='work_budget', box=None)
      self.detector.reset()
      self.next_sample = time.monotonic() + .5
    record = dict(version=1, frame_id=frame_id, timestamp=stamp, stream='road', width=1344, height=760,
                  control_permission=False, work_ms=work_ms, **result)
    temp = PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(record, allow_nan=False))
    temp.replace(PATH)
    if logger is not None and now >= self.next_log:
      logger('leadBrakeLights', **record)
      self.next_log = now + 1.
    return record


def main():
  from openpilot.common.params import Params
  params = Params()
  if not requested(params):
    return
  from msgq.visionipc import VisionIpcClient, VisionStreamType
  from openpilot.common.swaglog import cloudlog
  import cv2
  cv2.setNumThreads(1)
  cv2.ocl.setUseOpenCL(False)
  client = VisionIpcClient('camerad', VisionStreamType.VISION_STREAM_ROAD, True)
  worker = BrakeRuntime()
  try:
    while requested(params):
      start, cpu = time.monotonic(), time.process_time()
      if not client.is_connected():
        worker = BrakeRuntime()
        client.connect(False)
      try:
        frame = client.recv(timeout_ms=0) if client.is_connected() else None
        if frame is not None:
          worker.update(frame, client, params, logger=cloudlog.event)
      except Exception:
        cloudlog.exception('leadBrakeLightsObservationFailed')
      wall, used = time.monotonic()-start, time.process_time()-cpu
      time.sleep(max(0., .1-wall, 5*used-wall))
  finally:
    PATH.unlink(missing_ok=True)


if __name__ == '__main__':
  main()
