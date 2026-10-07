import ast
import base64
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.common.jetson_status import JetsonConnectivity
from openpilot.tools.jetson import hud, snapshot
from openpilot.tools.jetson.snapshot import parking_frames

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'openpilot/selfdrive/carrot/cluster'))
from cluster_parking import NexoParkingTracker


def packet():
  event = log.Event.new_message(logMonoTime=100_000_000_000, valid=True)
  event.init('can', 1)
  event.can[0] = {'address': 0x4F4, 'src': 0, 'dat': bytes.fromhex('0025400040010101')}
  return {'version': 1, 'sent': 100.1, 'updated': 1000.1, 'session': 'a' * 32,
          'events': {'can': base64.b64encode(event.to_bytes()).decode()}, 'params': {}, 'cameras': {},
          'mono': {'can': 100_000_000_000}, 'received': {'can': 100.},
          'valid': {'can': True}, 'alive': {'can': True}}


def test_parking_keeps_original_bits_and_age_across_different_boot_clocks(monkeypatch):
  value = packet()
  monkeypatch.setattr(hud, 'read_snapshot', lambda: value)
  sm = hud.RemoteSubMaster(['carState', 'can', 'sendcan', 'liveTracks'])
  sm.update()
  sock = hud.ParkingSocket()
  received = sock.receive_event()
  assert received.logMonoTime == 1_000_000_000_000
  assert received.can[0].dat == bytes.fromhex('0025400040010101')
  tracker = NexoParkingTracker()
  tracker.observe(received.can, received.logMonoTime / 1e9, 1000.1, received.valid)
  assert tracker.current(1000.1).front_codes == (0, 1, 1, 1, 0)
  assert sock.receive_event() is None
  assert sm.updated['can']
  assert not sm.alive['sendcan']
  sm.update()
  assert not sm.updated['can']
  # A repeated old frame never gets a new receive timestamp.
  value['updated'], value['sent'] = 1002.1, 102.1
  sm.update()
  assert sock.receive_event() is None
  assert not any(tracker.current(1002.1).front_codes)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  sm.update()
  assert not any(sm.alive.values())
  assert sock.receive_event() is None


def test_reboot_same_can_timestamp_is_new_generation(monkeypatch):
  value = packet()
  monkeypatch.setattr(hud, 'read_snapshot', lambda: value)
  sock = hud.ParkingSocket()
  assert sock.receive_event()
  value['session'] = 'b' * 32
  assert sock.receive_event()


def test_only_receive_bus_zero_spas12_is_forwarded():
  def frame(address=0x4F4, bus=0, length=8):
    return SimpleNamespace(address=address, src=bus, dat=bytes(length))
  wanted = frame()
  assert parking_frames([wanted, frame(bus=1), frame(bus=128), frame(address=0x420), frame(length=7)]) == [wanted]


def test_snapshot_rejects_control_and_bad_time():
  value = packet()
  assert snapshot.validate_snapshot(value) is value
  value['events']['sendcan'] = ''
  with pytest.raises(ValueError):
    snapshot.validate_snapshot(value)
  del value['events']['sendcan']
  value['sent'] = float('nan')
  with pytest.raises(ValueError):
    snapshot.validate_snapshot(value)


def test_display_params_preserve_values_without_remote_writes(monkeypatch):
  value = packet()
  value['params'] = {'ClusterHudSideCameraWidth': base64.b64encode(b'39').decode()}
  monkeypatch.setattr(hud, 'read_snapshot', lambda: value)
  params = hud.DisplayParams()
  assert params.get_int('ClusterHudSideCameraWidth') == 39
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  assert params.get_int('ClusterHudSideCameraWidth') == 39
  with pytest.raises(ValueError):
    params.put_bool_nonblocking('OpenpilotEnabledToggle', False)
  assert not hasattr(params, 'put')


def test_navigation_display_clock_and_typed_params(monkeypatch):
  value = packet()
  value['params']['CarrotNaviDebug'] = snapshot.encode(json.dumps({'receivedMono': 100., 'title': 'Navi'}))
  value['params']['LiveParameters'] = snapshot.encode({'steerRatio': 15.5})
  monkeypatch.setattr(hud, 'read_snapshot', lambda: value)
  params = hud.DisplayParams()
  assert json.loads(params.get('CarrotNaviDebug'))['receivedMono'] == 1000.
  assert json.loads(params.get('LiveParameters')) == {'steerRatio': 15.5}
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  assert json.loads(params.get('CarrotNaviDebug'))['receivedMono'] == 1000.


def test_all_display_keys_and_services_exist():
  keys = set(re.findall(r'\{"([A-Za-z0-9]+)"', (ROOT / 'openpilot/common/params_keys.h').read_text()))
  assert set(snapshot.PARAMS + snapshot.MEMORY_PARAMS) <= keys
  from openpilot.cereal.services import SERVICE_LIST
  assert set(snapshot.SERVICES) <= set(SERVICE_LIST)
  # This correspondence is checked without importing the native HUD runtime.
  tree = ast.parse((ROOT / 'openpilot/selfdrive/carrot/cluster/cluster_live.py').read_text(encoding='utf-8'))
  live = next(ast.literal_eval(n.value) for n in tree.body
              if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'LIVE_SERVICES_BASE' for t in n.targets))
  assert set(live) == set(snapshot.SERVICES)


def test_wifi_and_usb_badge_leases_are_independent():
  status = JetsonConnectivity()
  status.update({'magic': 'NEXO_JETSON_STATUS', 'comma_tcp': True}, 10)
  status.update({'magic': 'NEXO_JETSON_STATUS', 'transport': 'usb', 'usb_connected': False}, 11)
  assert status.connected(11)
  assert not status.connected(12.5)
  status.update({'magic': 'NEXO_JETSON_STATUS', 'transport': 'usb', 'usb_connected': True}, 13)
  assert status.connected(14)
  assert not status.connected(16)
