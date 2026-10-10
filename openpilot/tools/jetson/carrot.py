"""Read-only JLNK v2 companion session for the Windows carrot-jetson image.

No engine request, inference, upload, shutdown, remote Params or DM messages.
The existing NEXD host announces itself; Carrot waits for HELLO.
"""
import json
import struct
import time

from openpilot.tools.jetson.state import STATUS, atomic_json, decode_json, finite, public_telemetry, public_text
from openpilot.tools.jetson.transport import protocol as nexd
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout

HUD_LIMIT = 96 * 1024
DISPLAY_SERVICES = ('carState', 'modelV2', 'radarState', 'controlsState', 'selfdriveState', 'carParams',
                    'liveCalibration', 'roadCameraState', 'wideRoadCameraState', 'deviceState',
                    'carrotNavi', 'carrotMan', 'navInstructionCarrot', 'navInstruction', 'liveTracks',
                    'longitudinalPlan', 'lateralPlan', 'carControl', 'livePose', 'navRoute')


def display_packet(raw, now):
  """Keep a small road preview before optional display events exceed the cap."""
  from openpilot.tools.jetson.snapshot import PARAMS, MEMORY_PARAMS, MAX_SNAPSHOT, validate_snapshot
  value = validate_snapshot(decode_json(raw, MAX_SNAPSHOT))
  if not 0 <= now - value['sent'] < .3:
    raise ValueError('HUD snapshot expired')
  selected = {name: value['events'][name] for name in DISPLAY_SERVICES if name in value['events']}
  # No new raw CAN or driver camera forwarding on the carrot companion path.
  result = {'version': 1, 'sent': value['sent'], 'events': selected,
            'params': {k: v for k, v in value['params'].items() if k in PARAMS + MEMORY_PARAMS}, 'cameras': {}}
  for key in ('received', 'mono', 'valid', 'alive'):
    result[key] = {name: item for name, item in value[key].items() if name in selected}
  camera = value['cameras'].get('road')
  if (isinstance(camera, dict) and camera.get('width') == 384 and camera.get('height') == 240
      and finite(camera.get('time')) and 0 <= now - camera['time'] < .3
      and isinstance(camera.get('jpeg'), str) and len(camera['jpeg']) <= 4 * ((16 * 1024 + 2) // 3)):
    result['cameras']['road'] = {k: camera[k] for k in ('width', 'height', 'frame', 'time', 'jpeg')}
  while True:
    payload = json.dumps(result, allow_nan=False, separators=(',', ':')).encode()
    if len(payload) <= HUD_LIMIT:
      return payload, result['cameras']
    if not selected:
      raise ValueError('HUD settings exceed bounded allocation')
    # Least important display event first; the preview is never silently dropped.
    name, _ = selected.popitem()
    for key in ('received', 'mono', 'valid', 'alive'):
      result[key].pop(name, None)


class CompanionWire:
  HEADER_SIZE = nexd.HEADER_SIZE
  GADGET_TX_ALIGN = nexd.GADGET_TX_ALIGN
  PACKET_MULTIPLE = nexd.PACKET_MULTIPLE
  Flag = nexd.Flag
  ProtocolError = nexd.ProtocolError

  def __init__(self):
    self.mode = None

  def unpack_header(self, raw):
    fields = struct.unpack_from(nexd.HEADER_FMT, raw)
    magic, version, kind, _, flags, _, reserved = fields
    if magic == nexd.MAGIC and version == nexd.VERSION:
      mode = 'nexd'
      nexd.unpack_header(raw)
    elif magic == 0x4B4E4C4A and version == 2:
      mode = 'carrot'
      if kind not in (2, 4, 7, 13, 14, 16) or flags & ~128 or reserved:
        raise self.ProtocolError('unexpected Carrot companion response')
    else:
      raise self.ProtocolError('unsupported companion protocol')
    if self.mode is not None and self.mode != mode:
      raise self.ProtocolError('wire protocol changed during session')
    self.mode = mode
    return fields

  def pack_header(self, kind, seq, length, flags=0, reserved=0):
    if self.mode == 'nexd':
      return nexd.pack_header(kind, seq, length, flags, reserved)
    self.mode = 'carrot'
    if kind not in (1, 12, 15, 0x4000, 0x4001):
      raise self.ProtocolError('companion request outside allowlist')
    return struct.pack(nexd.HEADER_FMT, 0x4B4E4C4A, 2, int(kind), seq, flags, length, reserved)


def select(transport):
  wire = CompanionWire()
  transport.wire_protocol = wire
  try:
    message = transport.recv(.8)
  except LinkTimeout:
    # A partial legacy header already pins its protocol; don't send JLNK into it.
    if wire.mode == 'nexd' or getattr(getattr(transport, 'rx', None), 'available', 0):
      message = transport.recv(1.)
    else:
      return 'carrot', None
  if wire.mode != 'nexd' or message.msg_type != nexd.Msg.HEARTBEAT:
    raise LinkError('unexpected initial companion message')
  return 'nexd', message


def session(transport, should_run, publisher, sock, on_health=None):
  sequence = 0
  tx = {'session_connected': True, 'capability_negotiated': True}

  def query(kind, reply):
    nonlocal sequence
    sequence += 1
    seq = sequence
    transport.send(kind, seq, [b'{"client":{"name":"nexo-companion"}}' if kind == 1 else b'{}'], timeout=.5)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
      message = transport.recv(max(.001, deadline - time.monotonic()))
      if message.msg_type == 14:
        raise LinkError(str(decode_json(bytes(message.payload)).get('detail', 'Carrot server error'))[:240])
      if message.seq == seq and message.msg_type == reply:
        return decode_json(bytes(message.payload), 64 * 1024)
    raise LinkTimeout('Carrot companion telemetry expired')

  def publish(value):
    now = time.monotonic()
    record = {'magic': 'NEXO_JETSON_STATUS', 'transport': 'usb', 'protocol': 'carrot-v2',
              'updated': now, 'usb_connected': True, 'comma_connected': True, 'service_active': True,
              'model_ready': value.get('engine_state') == 'ready' or bool(value.get('loaded')), 'model_active': False,
              'host_telemetry': public_telemetry(value), 'state': 'connected', 'diagnosis': 'Carrot USB 상태 수신 · 모델 전환 없음',
              'last_receive_monotonic': now, **tx}
    try:
      atomic_json(STATUS, record)
    except OSError:
      pass  # Diagnostic file failure must not disconnect the companion session.
    raw = json.dumps(record, allow_nan=False).encode()
    for port in (8766, 8767, 8768):
      try:
        sock.sendto(raw, ('127.0.0.1', port))
      except OSError:
        pass
    if on_health is not None:
      on_health(True, now)

  peer = query(1, 2)
  if peer.get('protocol') != 2 or not (peer.get('carrot_host') == 'jetson' or peer.get('backend') == 'trt'):
    raise LinkError('peer is not a supported carrot-jetson v2 host')
  tx['capabilities'] = {name: peer.get(name) is True for name in ('carrot_hud_v1', 'carrot_navi_v1', 'carrot_wifi_v1')}
  publish(peer)
  epoch = time.monotonic_ns()
  media_id = 0
  next_state = next_hud = 0.
  while should_run():
    now = time.monotonic()
    if now >= next_state:
      publish(query(12, 13))
      next_state = time.monotonic() + .5
    if peer.get('carrot_hud_v1') is True and now >= next_hud:
      raw = None
      cameras = {}
      try:
        source = publisher.snapshot(True)
        if source:
          raw, cameras = display_packet(source, time.monotonic())
        tx['display_error'] = '' if raw else 'HUD/카메라 snapshot 대기'
        metrics = getattr(publisher, 'metrics', {})
        tx['preview_metrics'] = {k: v for k, v in metrics.items() if k in (
          'fps', 'cpu_percent', 'preview_latency_ms', 'frames', 'errors', 'target_fps') and finite(v)}
      except Exception as error:
        tx['display_error'] = public_text(f'HUD snapshot: {type(error).__name__}: {error}')
      if raw:
        sequence += 1
        started = time.monotonic()
        transport.send(0x4000, sequence, [raw], timeout=.5)
        tx.update(last_hud_tx_mono=time.monotonic(), hud_bytes=len(raw), hud_send_ms=(time.monotonic() - started) * 1000)
        if cameras:
          tx.update(last_camera_tx_mono=tx['last_hud_tx_mono'], camera_frame_mono=cameras['road']['time'],
                    camera_tx_latency_ms=(tx['last_hud_tx_mono'] - cameras['road']['time']) * 1000)
      next_hud = now + .1
    if peer.get('carrot_navi_v1') is True:
      # Reuse the existing display-media source; never alter navigation control.
      for kind, raw in publisher.media(False):
        if kind != nexd.Msg.NAVI_MEDIA or not 0 < len(raw) <= 1024 * 1024:
          continue
        media_id += 1
        deadline = time.monotonic() + .5
        for offset in range(0, len(raw), 32 * 1024):
          sequence += 1
          remaining = deadline - time.monotonic()
          if remaining <= 0:
            raise LinkTimeout('Carrot navigation display transfer expired')
          header = struct.pack('<QQII', epoch, media_id, offset, len(raw))
          transport.send(0x4001, sequence, [header, raw[offset:offset + 32 * 1024]], timeout=remaining)
    time.sleep(.02)
