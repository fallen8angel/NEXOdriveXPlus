import json
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.carrot import lead_brake_lights as runtime
from tools.signal_analysis.lead_brake_detector import LeadBrakeDetector  # noqa: TID251


def context():
  lead = dict(status=True, radar=True, radarTrackId=3, dRel=8., yRel=0.)
  vision = dict(prob=.99, x=[9.52], y=[0.])
  car = dict(yawRate=0., steeringAngleDeg=0., vEgo=0.)
  calibration = dict(calStatus='calibrated', height=[1.286], rpyCalib=[0., 0., 0.])
  return lead, vision, car, calibration


def lamp_image():
  rgb = np.zeros((760, 1344, 3), np.uint8)
  rgb[342:348, 660:684, 0] = 255
  return rgb


def test_confirmation_and_missing_lamp_is_unknown_not_off():
  detector = LeadBrakeDetector()
  for t in [1., 1.1]:
    assert detector.process(lamp_image(), t, *context())['state'] == 'unknown'
  assert detector.process(lamp_image(), 1.2, *context())['state'] == 'candidate'
  assert detector.process(np.zeros((760, 1344, 3), np.uint8), 1.25, *context())['state'] == 'unknown'


@pytest.mark.parametrize('kind', ['turn', 'vision', 'lead', 'calibration', 'nan'])
def test_rejects_unreliable_context(kind):
  lead, vision, car, calibration = context()
  if kind == 'turn':
    car['yawRate'] = .2
  if kind == 'vision':
    vision['y'] = [2.]
  if kind == 'lead':
    lead['status'] = False
  if kind == 'calibration':
    calibration['calStatus'] = 'uncalibrated'
  if kind == 'nan':
    lead['dRel'] = float('nan')
  detector = LeadBrakeDetector()
  for t in [1., 1.1, 1.2]:
    assert detector.process(lamp_image(), t, lead, vision, car, calibration)['state'] == 'unknown'


def test_neighbor_and_low_tail_lamps_do_not_confirm():
  rgb = np.zeros((760, 1344, 3), np.uint8)
  rgb[342:348, 900:924, 0] = 255
  rgb[410:416, 660:684, 0] = 255
  detector = LeadBrakeDetector()
  for t in [1., 1.1, 1.2]:
    assert detector.process(rgb, t, *context())['state'] == 'unknown'


def test_repeated_frame_and_replacement_lead_do_not_inherit_confirmation():
  detector = LeadBrakeDetector()
  for _ in range(10):
    assert detector.process(lamp_image(), 1., *context())['state'] == 'unknown'
  for t in [1.1, 1.2]:
    detector.process(lamp_image(), t, *context())
  lead, *rest = context()
  lead['radarTrackId'] = 4
  assert detector.process(lamp_image(), 1.3, lead, *rest)['state'] == 'unknown'


def test_long_gap_requires_new_confirmation():
  detector = LeadBrakeDetector()
  for t in [1., 1.1, 1.2]:
    detector.process(lamp_image(), t, *context())
  assert detector.process(lamp_image(), 1.6, *context())['state'] == 'unknown'




def test_off_has_no_subscription_conversion_detector_or_files(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime, 'PATH', tmp_path/'result.json')
  params = SimpleNamespace(get_bool=lambda key: False)
  worker = runtime.BrakeRuntime()
  assert worker.update(None, None, params) is None
  assert worker.sm is None and worker.detector is None and not list(tmp_path.iterdir())
  monkeypatch.setattr(runtime, 'read_record', lambda *a: pytest.fail('OFF file polling'))
  assert runtime.BrakeDisplayReader().read(params, now=1.) is None


@pytest.mark.parametrize('frame,stamp', [(None, 1.), (SimpleNamespace(width=1344,height=760), .1),
                                       (SimpleNamespace(width=1928,height=1208), 1.)])
def test_stale_missing_or_wrong_camera_is_rejected_before_conversion(frame, stamp):
  worker = runtime.BrakeRuntime()
  assert worker.update(frame, SimpleNamespace(frame_id=1,timestamp_eof=int(stamp*1e9)),
                       SimpleNamespace(get_bool=lambda key: True), now=1.1) is None
  assert worker.sm is None and worker.detector is None


def test_duplicate_or_reversed_frames_rejected_without_context_subscription():
  worker = runtime.BrakeRuntime()
  worker.last_frame, worker.last_stamp = 9, 1.
  params = SimpleNamespace(get_bool=lambda key: True)
  frame = SimpleNamespace(width=1344,height=760)
  assert worker.update(frame, SimpleNamespace(frame_id=9,timestamp_eof=1050000000),params,now=1.1) is None
  assert worker.update(frame, SimpleNamespace(frame_id=10,timestamp_eof=990000000),params,now=1.1) is None
  assert worker.sm is None


@pytest.mark.parametrize('change', [{'timestamp':0.}, {'stream':'wideRoad'}, {'control_permission':True},
                                   {'state':'off'}, {'width':1928}, {'frame_id':True}, {'box':[1,2,3,float('nan')]}])
def test_invalid_or_stale_display_records_never_hold(change):
  record = dict(version=1,frame_id=20,timestamp=1.,control_permission=False,state='candidate',
                width=1344,height=760,stream='road',box=[600,340,640,350])
  assert runtime.display_result(record,1.1,(20,1.05,1344,760,False)) is record
  assert runtime.display_result({**record,**change},1.1) is None
  assert runtime.display_result(record,1.1,(20,1.05,1344,760,True)) is None
  assert runtime.display_result(record,1.4) is None


def test_blinker_and_stopped_sharp_turn_are_unknown():
  lead, vision, car, calibration = context()
  for change in [{'leftBlinker':True},{'rightBlinker':True},{'steeringAngleDeg':20.}]:
    detector = LeadBrakeDetector()
    for t in (1.,1.1,1.2):
      assert detector.process(lamp_image(),t,lead,vision,{**car,**change},calibration)['state']=='unknown'


def test_default_off_and_single_setting():
  from pathlib import Path
  root = Path(__file__).resolve().parents[3]
  settings = json.loads((root/'selfdrive/carrot_settings.json').read_text(encoding='utf8'))
  def objects(value):
    if isinstance(value,dict):
      yield value
      for v in value.values():
        yield from objects(v)
    elif isinstance(value,list):
      for v in value:
        yield from objects(v)
  definitions=[v for v in objects(settings) if v.get('name')=='LeadBrakeLights']
  assert len(definitions)==1 and definitions[0]['default']==0
  assert '{"LeadBrakeLights", {PERSISTENT, BOOL, "0"}}' in (root/'common/params_keys.h').read_text(encoding='utf8')


def test_recycled_camera_buffer_is_rejected_before_subscribing(monkeypatch):
  from openpilot.selfdrive.modeld import signal_tracking_shadow
  client=SimpleNamespace(frame_id=9,timestamp_eof=1000000000)
  def copy(frame):
    frame.frame_id=10
    return lamp_image()
  monkeypatch.setattr(signal_tracking_shadow,'copy_nv12_rgb',copy)
  monkeypatch.setattr(runtime.time,'monotonic',lambda:1.1)
  worker=runtime.BrakeRuntime()
  assert worker.update(SimpleNamespace(width=1344,height=760,frame_id=9),client,SimpleNamespace(get_bool=lambda key:True)) is None
  assert worker.sm is None


def test_real_runtime_publishes_only_candidate_unknown_with_fresh_context(monkeypatch,tmp_path):
  from openpilot.selfdrive.modeld import signal_tracking_shadow
  monkeypatch.setattr(signal_tracking_shadow,'copy_nv12_rgb',lambda frame:lamp_image())
  monkeypatch.setattr(runtime,'PATH',tmp_path/'result.json')
  clock=[1.]
  monkeypatch.setattr(runtime.time,'monotonic',lambda:clock[0])
  lead,vision,car,cal=context()
  messages={k:SimpleNamespace(to_dict=lambda v=v:v) for k,v in
            [('carState',car),('liveCalibration',cal)]}
  messages['radarState']=SimpleNamespace(leadOne=SimpleNamespace(to_dict=lambda:lead))
  messages['modelV2']=SimpleNamespace(leadsV3=[SimpleNamespace(to_dict=lambda:vision)])
  class Context:
    seen=valid=dict.fromkeys(messages,True)
    logMonoTime=dict.fromkeys(messages,1000000000)
    def update(self,timeout):
      self.logMonoTime=dict.fromkeys(messages,int(clock[0]*1e9))
    def __getitem__(self,key):
      return messages[key]
  worker=runtime.BrakeRuntime()
  worker.sm,worker.detector=Context(),LeadBrakeDetector()
  logs=[]
  for i,stamp in enumerate((1.,1.11,1.22)):
    clock[0]=stamp
    result=worker.update(SimpleNamespace(width=1344,height=760,frame_id=i),
                         SimpleNamespace(frame_id=i,timestamp_eof=int(stamp*1e9)),
                         SimpleNamespace(get_bool=lambda key:True),logger=lambda *a,**k:logs.append(k))
  assert result['state']=='candidate' and result['control_permission'] is False
  assert json.loads(runtime.PATH.read_text())==result and logs
  clock[0]=1.34
  car['leftBlinker']=True
  result=worker.update(SimpleNamespace(width=1344,height=760,frame_id=9),SimpleNamespace(frame_id=9,timestamp_eof=1340000000),
                       SimpleNamespace(get_bool=lambda key:True))
  assert result['state']=='unknown'
