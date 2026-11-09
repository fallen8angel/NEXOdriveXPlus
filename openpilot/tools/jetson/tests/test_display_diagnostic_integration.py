from types import SimpleNamespace as NS
import json
import pytest

from openpilot.selfdrive.carrot.cluster.signal_display import live_signal, nav_phase
from openpilot.selfdrive.carrot.server.services.can_health import CanHealth
from openpilot.tools.jetson import ncm_display, health, hud_events, hud


class Services(dict):
  def sample(self, now):
    self.valid = self.alive = self.updated = dict.fromkeys(self, True)
    self.logMonoTime = dict.fromkeys(self, round(now*1e9))
    return self


def test_panda_counts_history_growing_recovery_reset_and_missing_input():
  bus = NS(totalErrorCnt=88, busOff=False, errorPassive=False)
  panda = NS(uptime=90, canState0=bus, canState1=bus, canState2=bus)
  sm = Services(pandaStates=[panda], carState=NS(canValid=True, canTimeout=False, accFaulted=True,
                                                 steerFaultTemporary=False, steerFaultPermanent=False))
  tracker = CanHealth()
  def sample(t):
    tracker.observe(sm.sample(t), t+.001)
    return tracker.report(t+.001)
  assert sample(10)['state'] == 'unknown'
  report = sample(10.1)
  assert report['state']=='normal' and report['ecu']['accFaulted']
  bus.totalErrorCnt += 2
  assert sample(10.2)['buses'][0]['rate_hz'] == 20
  bus.busOff = True
  assert sample(10.4)['state']=='error'
  bus.busOff = False
  for t in (10.7, 11.2, 11.8, 12.3):
    assert sample(t)['state']=='recovering'
  assert sample(12.5)['buses'][0]['recovered']
  assert tracker.report(14)['state']=='unknown' and tracker.report(14)['ecu'] is None
  panda.uptime = 1
  bus.totalErrorCnt = 900
  assert sample(14)['buses'][0]['rate_hz'] is None
  sm['pandaStates'] = []
  sm['carState'].canValid = False
  assert sample(14.1)=={'state':'unknown','buses':[],'ecu':None}


@pytest.mark.parametrize('block', ['leadOne','leadTwo','invalid','not_drive','old_radar','moving','no_plan'])
def test_signal_lamp_does_not_claim_signal_stop_without_fresh_no_lead_standstill(block):
  sm = Services(longitudinalPlan=NS(trafficState=1, shouldStop=True, hasLead=False),
                radarState=NS(leadOne=NS(status=False),leadTwo=NS(status=False)),
                carState=NS(canValid=True,canTimeout=False,gearShifter='drive',standstill=True,vEgo=0)).sample(5)
  assert live_signal(sm,5.1)==(1,True)
  if block in ('leadOne','leadTwo'):
    getattr(sm['radarState'],block).status = True
  elif block=='invalid':
    sm['carState'].canValid=False
  elif block=='not_drive':
    sm['carState'].gearShifter='park'
  elif block=='old_radar':
    sm.logMonoTime['radarState']=3_000_000_000
  elif block=='moving':
    sm['carState'].standstill=False
  else:
    sm['longitudinalPlan'].shouldStop=False
  assert live_signal(sm,5.1)==(1,False)
  assert live_signal(sm,5.6)==(0,False)
  assert live_signal(sm,5.1,'yellow')==(3,False)
  for stamp, result in [(5,'yellow'),(0,None),(9,None),(2,None)]:
    assert nav_phase(json.dumps(dict(lamp='yellow',remain=2,ts=stamp)),5.1)==result


def test_reconnect_releases_previous_session_observations(tmp_path,monkeypatch):
  monkeypatch.setattr(ncm_display, 'STATUS', tmp_path/'state')
  monkeypatch.setattr(ncm_display, '_diagnostic_state', None)
  monkeypatch.setattr(ncm_display, '_last_observations', {})
  ncm_display.publish('connected',peer={'source_revision':'old'},last_peer_rx_mono=4.)
  ncm_display.publish('retrying',error='timeout',error_domain='tcp',usb_link={'carrier':'1'})
  record=json.loads(ncm_display.STATUS.read_text())
  assert 'peer' not in record and 'last_peer_rx_mono' not in record
  assert record['diagnostic_events'][-1]['domain']=='tcp'
  ncm_display.publish('connected',hello_complete=True)
  assert 'peer' not in json.loads(ncm_display.STATUS.read_text())
  assert hud.failure_reason(hud_events.EncoderStalled('blocked'))=='encoder_output_stalled'


def test_historical_expiry_age_is_measured_on_two_local_clocks(tmp_path,monkeypatch):
  from openpilot.tools.jetson.state import atomic_json
  monkeypatch.setattr(health,'RUNTIME',tmp_path)
  peer=dict(hud_rx_age=.1,hud_pipeline=dict(trace={'encoder_last_expiry_age_s':15.,'panel_gap_count':8},
            panel_write_recent=True,last_successful_panel_age_s=.1,display_mode='vehicle'))
  atomic_json(tmp_path/'ncm-display.json',dict(updated=10.,state='connected',last_peer_rx_mono=10.,peer=peer))
  assert health.diagnostic_evidence(10.25)['encoder_expiry_age_s']==15.25
  assert health.diagnostic_evidence(13)['encoder_expiry_age_s'] is None
