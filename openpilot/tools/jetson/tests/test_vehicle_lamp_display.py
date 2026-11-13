"""Forward optical evidence fixtures; these do not validate real lamp recognition."""
import base64
import io
import numpy as np
import pytest
from PIL import Image
from openpilot.tools.jetson import lamp_settings, lamp_worker, vehicle_lamps as vl
from openpilot.tools.jetson.state import atomic_json


def pixels(center=False, left=False, right=False):
  rgb = np.full((100, 160, 3), 28, dtype=np.uint8)
  for x in (25, 125):
    rgb[51:58, x:x+10] = [235, 25, 20]
  if center:
    rgb[22:26, 69:91] = [245, 21, 19]
  for x, lit in ((24, left), (128, right)):
    if lit:
      rgb[40:46, x:x+8] = [248, 155, 16]
  return rgb


def packet(index, rgb, targets=None, session='a'*32):
  data = io.BytesIO()
  Image.fromarray(rgb).save(data, format='JPEG', quality=88)
  t = 30 + index * .1
  return {'session': session, 'sent': t+.02, 'updated': t+.03, 'vehicle_lamps': dict.fromkeys(lamp_settings.KEYS, True),
          'cameras': {'road': dict(time=t, frame=index, jpeg=base64.b64encode(data.getvalue()).decode(),
                                   width=160, height=100)},
          'lamp_targets': [dict(id=3, box=[0.,0.,1.,1.], d=12, y=0)] if targets is None else targets}


@pytest.mark.parametrize('side,expected', [('left',2), ('right',4), ('both',8)])
def test_actual_jpeg_to_timed_track_and_current_blink_phase(side, expected):
  analyzer = lamp_worker.Analyzer()
  for i in range(14):
    lit = i in (2,3,4,8,9,10)
    snap = packet(i, pixels(left=lit and side in ('left','both'), right=lit and side in ('right','both')))
    result = analyzer.analyze(snap, None, snap['updated'])
    assert [row['mask'] for row in result['tracks']] == ([expected] if i in (8,9,10) else [])


def test_brake_requires_off_then_red_pair_and_high_center_and_clears_on_off():
  a = lamp_worker.Analyzer()
  for i, (lit, expected) in enumerate([(True,0), (True,0), (False,0), (True,0), (True,1), (False,0)]):
    snap = packet(i, pixels(center=lit))
    assert [r['mask'] for r in a.analyze(snap, None, snap['updated'])['tracks']] == ([1] if expected else [])


def test_ambiguous_old_missing_and_new_identity_do_not_inherit_previous_lights(tmp_path, monkeypatch):
  a = lamp_worker.Analyzer()
  for i in range(3):
    snap = packet(i, pixels(center=i>0))
    out = a.analyze(snap, None, snap['updated'])
  assert out['tracks']
  monkeypatch.setattr(lamp_worker,'OUTPUT',tmp_path/'result')
  atomic_json(lamp_worker.OUTPUT, out)
  assert lamp_worker.visible(snap, snap['updated'])
  assert not lamp_worker.visible(snap, snap['updated']+.4)
  assert not lamp_worker.visible({**snap, 'session':'b'*32}, snap['updated'])
  assert not lamp_worker.visible(packet(3,pixels()), snap['updated'])
  snap['vehicle_lamps']['brake'] = False
  assert lamp_worker.visible(snap, snap['updated'])[0]['mask'] == 0
  target = snap['lamp_targets'][0]
  for i, targets in enumerate(([target, {**target,'id':4}], [], [{**target, 'id':4}]), 3):
    value = packet(i, pixels(True), targets)
    assert not a.analyze(value, None, value['updated'])['tracks']


def test_gate_generic_yolo_only_on_same_exposure_and_class_contract():
  a = lamp_worker.Analyzer()
  for i in range(4):
    snap = packet(i, pixels(i>0))
    yolo = dict(road_capture_ns=round(snap['cameras']['road']['time']*1e9), objects=[], vehicle_detector=i==3)
    result = a.analyze(snap, yolo, snap['updated'])
    assert bool(result['tracks']) == (i==2)


def test_off_missing_prefs_and_hazard_never_becomes_one_turn(tmp_path, monkeypatch):
  monkeypatch.setattr(lamp_settings, 'PATH', tmp_path/'settings')
  assert not any(lamp_settings.read().values())
  with pytest.raises(ValueError):
    lamp_settings.save({'enabled': 'true'})
  prefs = dict(enabled=True, brake=False, turn=True, hazard=False)
  lamp_settings.save(prefs)
  assert lamp_settings.read() == prefs and vl.allowed(8,prefs)==0 and vl.allowed(5,prefs)==4
  worker = lamp_worker.Worker()
  monkeypatch.setattr(lamp_worker.subprocess,'Popen',lambda *a,**k: pytest.fail('disabled worker started'))
  worker.tick({})


def test_new_camera_epoch_recovers_after_frame_id_rolls_back():
  a = lamp_worker.Analyzer()
  for i in (61,62,63):
    snap=packet(i,pixels(i>61))
    result=a.analyze(snap,None,snap['updated'])
  assert result['tracks']
  for j, lit in enumerate((True,False,True,True)):
    snap=packet(64+j,pixels(lit))
    snap['cameras']['road']['frame']=j
    result=a.analyze(snap,None,snap['updated'])
    assert bool(result['tracks']) == (j==3)


def test_optional_onnx_requires_rear_and_photometric_confirmation(tmp_path):
  import cv2
  import onnx
  from onnx import TensorProto, helper, numpy_helper
  # Real OpenCV DNN session, deterministic untrained outputs for contract testing.
  x = helper.make_tensor_value_info('rgb', TensorProto.FLOAT, [1,3,64,96])
  y = helper.make_tensor_value_info('scores', TensorProto.FLOAT, [1,4])
  weights = numpy_helper.from_array(np.zeros((4,3), dtype=np.float32),'w')
  bias = numpy_helper.from_array(np.array([1,1,0,0], dtype=np.float32),'b')
  nodes = [helper.make_node('GlobalAveragePool',['rgb'],['pool']),
           helper.make_node('Flatten',['pool'],['flat']),
           helper.make_node('Gemm',['flat','w','b'],['scores'],transB=1)]
  model = helper.make_model(helper.make_graph(nodes,'contract',[x],[y],[weights,bias]),
                            opset_imports=[helper.make_opsetid('',13)])
  path=tmp_path/'test.onnx'
  onnx.save(model,path)
  analyzer=lamp_worker.Analyzer(cv2.dnn.readNetFromONNX(str(path)))
  for i in range(12):
    snap=packet(i,pixels(center=i>=9, left=i in (2,3,4,8,9,10)))
    rows=analyzer.analyze(snap,None,snap['updated'])['tracks']
    assert [r['mask'] for r in rows] == ([1] if i>=10 else [])


def test_worker_launch_from_installed_release(monkeypatch):
  # systemd's working directory may not be the release root. The isolated
  # child must load the same protected release, without inheriting PYTHONPATH.
  import sys
  from pathlib import Path

  class Child:
    returncode = None

    def poll(self):
      return None

  calls = []
  monkeypatch.setattr(lamp_worker, 'enabled', lambda _: True)
  monkeypatch.setattr(lamp_worker.subprocess, 'Popen',
                      lambda args, **kwargs: calls.append((args, kwargs)) or Child())
  worker = lamp_worker.Worker()
  worker.tick({'vehicle_lamps': True})
  assert len(calls) == 1
  args, kwargs = calls[0]
  assert args[:3] == [sys.executable, '-I', '-c']
  assert "runpy.run_module('openpilot.tools.jetson.lamp_worker'" in args[3]
  assert args[4] == str(Path(lamp_worker.__file__).resolve().parents[3])
  assert args[5].isdigit()
  assert kwargs['stdin'] == lamp_worker.subprocess.DEVNULL
