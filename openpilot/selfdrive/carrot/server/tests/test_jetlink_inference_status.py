import json

import pytest

from openpilot.selfdrive.carrot.server.services import jetson


def values():
  return ({'state':'ready','updated':100.,'telemetry_updated':100.,'last_infer_monotonic':100.,
           'protocol':3,'transport':'usb','sha256':'a'*64,'verified_sha256':'a'*64,
           'timings':[1000,200,1500],'inference_latency_ms':2.,'reconnect_count':4,
           'last_connection_error':'late Jetson boot','last_hud_tx_mono':99.9,'last_navi_tx_mono':99.8,
           'peer':{'carrot_hud_connected':True}},
          {'updated':100.,'sha256':'a'*64,'active':True,'ready':True})


@pytest.mark.parametrize('version',[2,3])
@pytest.mark.parametrize('transport',['usb','tcp'])
def test_native_status_json_has_verified_protocol_timings_fallback_and_display(version,transport):
  link,model=values()
  link.update(protocol=version,transport=transport)
  result=jetson.native_details(link,model,100.1)
  assert json.loads(json.dumps(result,allow_nan=False))==result
  assert result['protocol']==f'Jetlink v{version}' and result['transport']==transport.upper()
  assert result['sha_match'] and result['model_active'] and result['model_ready']
  assert not result['local_fallback'] and result['gpu_ms']==1. and result['total_ms']==1.5
  assert result['hud_connected'] and result['reconnect_count']==4
  assert result['last_connection_error']=='late Jetson boot'


def test_expired_evidence_never_claims_active_or_timings():
  link,model=values()
  result=jetson.native_details(link,model,104.)
  assert not result['connected'] and not result['model_ready']
  assert result['model_active'] is None and result['gpu_ms'] is None
  assert result['hud_connected'] is None


def test_mismatch_error_and_local_fallback_reason():
  link,model=values()
  model.update(sha256='b'*64,active=False,error='safe modeld restart after disconnect')
  result=jetson.native_details(link,model,100.1)
  assert result['sha_match'] is False and result['state']=='오류'
  assert result['local_fallback'] and result['fallback_reason']==model['error']


@pytest.mark.parametrize('value',[None,True,4,'3'])
def test_invalid_protocol_not_shown_as_connected_version(value):
  link,model=values()
  link['protocol']=value
  assert jetson.native_details(link,model,100.1)['protocol'] is None


def test_native_tcp_pipeline_reports_hud_navigation_without_claiming_usb(monkeypatch):
  link, model = values()
  link.update(transport='tcp', capability_negotiated=True, hud_enabled=True,
              peer={'carrot_hud_connected': True, 'carrot_health': {'age_s': 0., 'temp_c': 42.}})
  def record(path):
    if path == jetson.LINK_STATUS[0]:
      return link
    if path == jetson.MODEL_STATUS[0]:
      return model
    return {}
  monkeypatch.setattr(jetson, 'read_record', record)
  monkeypatch.setattr(jetson, 'usb_status', lambda: {'connected': False, 'usb3': None, 'speed': 'unavailable'})
  result = jetson.snapshot({}, now=100.1)
  assert json.loads(json.dumps(result, allow_nan=False)) == result
  assert result['connected'] and result['health']['transport'] == 'TCP'
  assert result['health']['temperature_c'] == 42.
  assert result['status']['usb_connected'] is False
  assert result['hud_tx_recent'] and result['capability_negotiated']
  assert result['recent']['inference_age_s'] == pytest.approx(.1)
  assert result['inference']['navigation_tx_age_s'] == pytest.approx(.3)
  stages = {stage['id']: stage['state'] for stage in result['stages']}
  assert stages['jetlink'] == stages['hud'] == 'OK'
  assert stages['usb'] != 'OK'


def test_native_status_exposes_current_conflict_separately_from_previous_retry():
  link, model = values()
  link.update(state='conflict', error='NexoJetsonUsb must be OFF')
  result = jetson.native_details(link, model, 100.1)
  assert result['last_error'] == 'NexoJetsonUsb must be OFF'
  assert result['last_connection_error'] == 'late Jetson boot'
  assert result['state'] == '오류' and result['model_active'] is None
