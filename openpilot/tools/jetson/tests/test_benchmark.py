"""Preview budgets, independent stages and fail-open optional display I/O."""
import base64
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from openpilot.selfdrive.carrot.server.services import jetson as status
from openpilot.tools.jetson import camera, carrot, host, manage, vehicle, yolo
from openpilot.tools.jetson.state import public_telemetry, public_text
from openpilot.tools.jetson.transport.base import Message

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def paths(monkeypatch, tmp_path):
  monkeypatch.setattr(status, 'DISPLAY_STATUS', tmp_path / 'display.json')
  monkeypatch.setattr(status, 'LINK_STATUS', (tmp_path / 'native.json',))
  monkeypatch.setattr(status, 'MODEL_STATUS', (tmp_path / 'model.json',))
  monkeypatch.setattr(status, 'UDC_ROOT', tmp_path / 'udcs')
  return tmp_path


def snapshot(now=100):
  return {'version': 1, 'sent': now, 'events': {}, 'params': {}, 'cameras': {},
          'mono': {}, 'received': {}, 'alive': {}, 'valid': {}}


def test_real_preview_matches_installed_decoder_and_keeps_full_view():
  width, height, stride = 640, 480, 672
  image = np.zeros((height * 3 // 2, stride), np.uint8)
  image[:height, :width] = np.arange(width, dtype=np.uint32) * 200 // width + 20
  image[height:, :width] = 128
  frame = SimpleNamespace(data=image.tobytes(), width=width, height=height, stride=stride, uv_offset=height * stride)
  client = SimpleNamespace(timestamp_eof=99_900_000_000, frame_id=7)
  preview = camera.sample_preview(frame, client, 'carrot', now=100)
  raw = base64.b64decode(preview['jpeg'])
  assert preview['width'] == 384 and preview['height'] == 240 and len(raw) <= 16 * 1024
  with Image.open(BytesIO(raw)) as received:
    assert received.size == (384, 240)
    pixels = np.asarray(received.convert('RGB'))
    assert pixels[:, :10].mean() < 40 and pixels[:, -10:].mean() > 190
  # Legacy preview geometry is unchanged.
  assert camera.jpeg_preview(frame)[:2] == (384, 288)


@pytest.mark.parametrize('stamp', [0, 101_000_000_000, 99_600_000_000])
def test_stale_camera_source_never_gets_a_fresh_encode_stamp(stamp):
  client = SimpleNamespace(timestamp_eof=stamp, frame_id=7)
  assert camera.sample_preview(None, client, 'carrot', now=100) is None


def test_large_route_cannot_drop_small_preview_or_add_can_dm():
  packet = snapshot()
  packet['events'] = {'carState': 'car', 'modelV2': 'm' * 60000, 'navRoute': 'n' * 120000, 'can': 'legacy-parking'}
  packet['cameras'] = {'road': {'width': 384, 'height': 240, 'time': 99.9, 'frame': 7, 'jpeg': 'a' * 16000},
                       'driver': {'jpeg': 'existing-legacy-display'}}
  raw, previews = carrot.display_packet(json.dumps(packet).encode(), 100)
  value = json.loads(raw)
  assert len(raw) <= carrot.HUD_LIMIT and previews['road']['frame'] == 7
  assert 'carState' in value['events'] and 'navRoute' not in value['events'] and 'can' not in value['events']
  assert set(value['cameras']) == {'road'}
  assert not any('driver' in name.lower() for name in carrot.DISPLAY_SERVICES)


def test_old_snapshot_and_camera_are_not_retimestamped():
  packet = snapshot(99)
  with pytest.raises(ValueError, match='expired'):
    carrot.display_packet(json.dumps(packet).encode(), 100)
  packet['sent'] = 100
  packet['cameras']['road'] = {'width': 384, 'height': 240, 'time': 99, 'frame': 7, 'jpeg': 'abc'}
  _, previews = carrot.display_packet(json.dumps(packet).encode(), 100)
  assert previews == {}


def test_display_and_diagnostic_file_failure_do_not_stop_state_queries(monkeypatch, tmp_path):
  clock = [100.]
  def sleep(delta):
    clock[0] += .6
  monkeypatch.setattr(carrot, 'time', SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=lambda: int(clock[0] * 1e9), sleep=sleep))
  responses = iter([Message(2, 1, 0, memoryview(b'{"protocol":2,"carrot_host":"jetson","carrot_hud_v1":true}')),
                    Message(13, 2, 0, memoryview(b'{}')), Message(13, 3, 0, memoryview(b'{}'))])
  sent, records = [], []
  transport = SimpleNamespace(send=lambda kind, *args, **kw: sent.append(kind), recv=lambda timeout: next(responses))
  def fail(*args):
    raise OSError('optional storage unavailable')
  monkeypatch.setattr(carrot, 'atomic_json', fail)
  turns = iter([True, True, False])
  carrot.session(transport, lambda: next(turns), SimpleNamespace(snapshot=fail),
                 SimpleNamespace(sendto=lambda raw, addr: records.append(json.loads(raw))))
  assert sent == [1, 12, 12] and len(records) == 9
  assert records[-1]['display_error'] and records[-1]['session_connected']


def test_stages_do_not_equate_ready_engine_with_inference_or_camera(paths):
  record = {'updated': 100, 'usb_connected': True, 'protocol': 'carrot-v2', 'model_ready': True,
            'session_connected': True, 'capability_negotiated': True, 'model_active': False,
            'last_receive_monotonic': 100, 'last_hud_tx_mono': 100, 'last_camera_tx_mono': 100,
            'camera_frame_mono': 99.9, 'host_telemetry': {'carrot_health': {'age_s': .1, 'temp_c': 54,
            'severity': 'ok', 'storage_mode': 'protected', 'addresses': [{'address': '192.0.2.3', 'interface': 'wlan0'}]}}}
  status.DISPLAY_STATUS.write_text(json.dumps(record))
  live = status.snapshot({'channels': {}}, 100.1)
  stages = {item['id']: item['state'] for item in live['stages']}
  assert stages == {'jetson': 'OK', 'usb': 'OK', 'jetlink': 'OK', 'camera': 'OK', 'inference': 'WAITING', 'hud': 'OK'}
  assert live['health']['temperature_c'] == 54 and live['status']['ip'] == '192.0.2.3'
  # A healthy heartbeat cannot renew old HUD or camera traffic.
  record.update(updated=101, last_receive_monotonic=101)
  status.DISPLAY_STATUS.write_text(json.dumps(record))
  delayed = status.snapshot({'channels': {}}, 101.1)
  stages = {item['id']: item['state'] for item in delayed['stages']}
  assert stages['jetlink'] == 'OK' and stages['camera'] == stages['hud'] == 'WAITING'
  expired = status.snapshot({'channels': {}}, 105)
  assert all(item['state'] == 'DISCONNECTED' for item in expired['stages'])
  assert expired['health']['temperature_c'] is None
  text = status.diagnostic_summary(live)
  assert 'JETSON USB: OK' in text and 'INFERENCE: WAITING' in text and 'JETSON TEMP: 54.0C' in text


def test_thermal_health_expires_independently_from_heartbeat(paths):
  packet = {'usb_connected': True, 'updated': 100, 'host_telemetry': {'carrot_health': {
            'age_s': 4, 'temp_c': 90, 'severity': 'error', 'reason': 'old sensor'}}}
  status.DISPLAY_STATUS.write_text(json.dumps(packet))
  value = status.snapshot({'channels': {}}, 100)
  assert value['connected'] and value['health']['temperature_c'] is None and not value['status']['last_error']


def test_public_health_never_contains_private_wifi_or_payload():
  value = public_telemetry({'protocol': 2, 'password': 'DO-NOT-LOG', 'profiles': [{'ssid': 'private'}],
      'carrot_health': {'temp_c': 54, 'reason': 'psk=DO-NOT-LOG', 'password': 'DO-NOT-LOG'}})
  assert 'DO-NOT-LOG' not in json.dumps(value) and 'profiles' not in value
  assert value['carrot_health']['temp_c'] == 54
  assert public_text('authorization: DO-NOT-LOG') == '[민감 정보 숨김]'


def test_remote_hud_packet_reports_receipt_without_logging_camera(monkeypatch, tmp_path):
  packet = snapshot()
  packet['cameras'] = {'road': {'width': 384, 'height': 240, 'time': 99.9, 'frame': 7, 'jpeg': 'private-preview-bytes'}}
  local = tmp_path / 'hud.packet'
  local.write_bytes(struct.pack('<d', 10.) + json.dumps(packet).encode())
  original = Path.open
  monkeypatch.setattr(Path, 'open', lambda path, *a, **kw: original(local if path.as_posix() == '/dev/shm/carrot-jetlink-hud.packet' else path, *a, **kw))
  received = manage.hud_receipt(10.1)
  assert received['camera_rx_age_s'] == pytest.approx(.2)
  assert 'jpeg' not in json.dumps(received) and 'private-preview' not in json.dumps(received)
  assert not manage.hud_receipt(10.6)


def test_detection_is_display_only_and_expires_on_source_change(monkeypatch, tmp_path):
  monkeypatch.setattr(yolo, 'RUNTIME', tmp_path)
  (tmp_path / 'video-input.json').write_text(json.dumps({'epoch': 'old'}))
  payload = {'magic': 'NEXO_JETSON_YOLO', 'epoch': 'old', 'width': 640, 'height': 480,
             'objects': [{'name': 'car', 'conf': .9, 'x1': 1, 'y1': 2, 'x2': 50, 'y2': 60}]}
  assert yolo.publish_objects(payload, 100)
  record = yolo.read_objects(100.1)
  assert record['display_only'] and record['objects'][0]['source'] == 'jetson-yolo'
  assert record['objects'][0]['class'] == 'car' and record['objects'][0]['confidence'] == .9
  assert yolo.read_objects(101) is None
  (tmp_path / 'video-input.json').write_text(json.dumps({'epoch': 'new'}))
  assert yolo.read_objects(100.1) is None and not yolo.publish_objects(payload, 100.2)


def test_diag_collector_stays_alive_if_stage_diagnostics_fail(monkeypatch, capsys):
  path = ROOT / 'openpilot/selfdrive/carrot/server/features/tools/nexo_jetson_diag.py'
  spec = importlib.util.spec_from_file_location('jetson_stage_diag_test', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  def fail(*args, **kwargs):
    raise OSError('diagnostic storage')
  monkeypatch.setattr(status, 'snapshot', fail)
  module._stage_summary({})
  assert '기존 진단 계속' in capsys.readouterr().out


def test_host_diagnostic_storage_failure_does_not_reset_video_source(monkeypatch):
  def fail(*args):
    raise OSError('read-only storage')
  monkeypatch.setattr(host, 'atomic_json', fail)
  assert host.diagnostic_write(Path('/unused'), {}) is False
  source = host.VideoSource(None, None, False)
  source.select(True)
  assert source.source == 'usb' and source.epoch


def test_dead_preview_worker_retries_with_budget_and_keeps_hud(monkeypatch):
  clock, spawned = [100.], []
  monkeypatch.setattr(vehicle, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
  def spawn(profile):
    spawned.append(profile)
    raise OSError('preview unavailable')
  monkeypatch.setattr(camera, 'CameraPublisher', spawn)
  publisher = vehicle.Publisher.__new__(vehicle.Publisher)
  publisher.builder = SimpleNamespace(params=SimpleNamespace(get_bool=lambda key: True), packet=lambda cameras: b'HUD')
  publisher.hud_connected, publisher.camera_profile, publisher.next_camera_attempt = True, 'carrot', 101.
  publisher.camera = SimpleNamespace(process=SimpleNamespace(poll=lambda: 1), close=lambda: None)
  assert publisher.snapshot(True) == b'HUD' and publisher.camera is None and not spawned
  clock[0] = 101
  assert publisher.snapshot(True) == b'HUD' and spawned == ['carrot']
  assert publisher.snapshot(True) == b'HUD' and spawned == ['carrot']
  clock[0] = 106
  assert publisher.snapshot(True) == b'HUD' and spawned == ['carrot', 'carrot']
