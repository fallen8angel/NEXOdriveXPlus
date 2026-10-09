"""Jetson host: USB first, one optional Wi-Fi video input, display-only outputs."""
import argparse
import json
import logging
import os
from pathlib import Path
import socket
import time
import uuid

from openpilot.tools.jetson.state import (Peer, RUNTIME, SNAPSHOT, STATUS, VideoGate, atomic_json,
                                         decode_json, display_status, finite, read_fresh)
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout
from openpilot.tools.jetson.transport.protocol import Msg
from openpilot.tools.jetson.fragments import Assembler
from openpilot.tools.jetson.retry import UsbRetry

log = logging.getLogger(__name__)


def temperature():
  values = []
  for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
    try:
      name = (zone / 'type').read_text().lower()
      value = float((zone / 'temp').read_text()) / 1000
      if any(part in name for part in ('cpu', 'gpu', 'soc')) and finite(value) and -40 <= value <= 150:
        values.append(value)
    except (OSError, ValueError, TypeError, UnicodeError):
      pass
  return max(values) if values else None


def local_ip(comma):
  if not comma:
    return ''
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
      sock.connect((comma, 9))
      return sock.getsockname()[0]
  except OSError:
    return ''


class VideoSource:
  def __init__(self, messaging, comma, enabled):
    self.messaging, self.comma, self.enabled = messaging, comma, enabled
    self.publisher = messaging.pub_sock('roadEncodeData') if enabled else None
    self.wifi = None
    self.source = None
    self.epoch = uuid.uuid4().hex
    self.gate = VideoGate()
    self.last_received = None

  def select(self, usb):
    source = 'usb' if usb else 'wifi' if self.comma else 'disconnected'
    if source == self.source:
      return
    self.wifi = None  # Close the old subscriber before starting a new source.
    self.source = source
    self.epoch = uuid.uuid4().hex
    self.gate.reset()
    self.last_received = None
    atomic_json(RUNTIME / 'video-input.json', {'updated': time.monotonic(), 'epoch': self.epoch, 'source': source})
    if self.enabled and source == 'wifi':
      self.wifi = self.messaging.sub_sock('roadEncodeData', addr=self.comma, conflate=False)

  def publish(self, raw):
    if not self.enabled:
      return
    event = self.messaging.log_from_bytes(raw)
    if event.which() != 'roadEncodeData':
      raise ValueError('wrong video service')
    data = event.roadEncodeData
    if self.gate.accept(int(data.idx.encodeId), bool(data.idx.flags & 8), bytes(data.header)):
      self.publisher.send(raw)
      self.last_received = time.monotonic()

  def poll_wifi(self):
    if self.wifi is not None:
      for _ in range(4):
        raw = self.wifi.receive(non_blocking=True)
        if raw is None:
          break
        self.publish(raw)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--comma', help='existing Wi-Fi fallback IP; omit for USB only')
  parser.add_argument('--video', action='store_true', help='export video to the existing local YOLO process')
  args = parser.parse_args()
  if args.comma in ('127.0.0.1', 'localhost', '0.0.0.0', '::1'):
    parser.error('fallback must be the Comma IP, not this host')
  # These publishers exist only on Jetson. No CAN/control service is published.
  os.environ['ZMQ'] = '1'
  from openpilot.cereal import messaging
  from openpilot.tools.jetson.transport.usbbulk import UsbBulkTransport
  messaging.reset_context()
  os.nice(5)
  video = VideoSource(messaging, args.comma, args.video)
  navi = messaging.pub_sock('carrotNaviMedia')
  udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  udp.setblocking(False)
  transport = None
  retry = UsbRetry(RUNTIME / 'host-usb-retry.json')
  retry.recover_after = 30.
  peer = Peer('comma')
  session = uuid.uuid4().hex
  sequence = 0
  next_connect = last_heartbeat = last_status = 0.
  connection_started = 0.
  last_data_sequence = -1
  assembler = Assembler()
  while True:
    now = time.monotonic()
    try:
      if transport is None and now >= next_connect and retry.ready(now):
        next_connect = now + 1
        # Missing hardware is waiting, not a failed USB/model load attempt.
        if UsbBulkTransport.present():
          retry.begin(now)
          transport = UsbBulkTransport.open()
          peer = Peer('comma')
          session, sequence = uuid.uuid4().hex, 0
          last_heartbeat, last_data_sequence = 0., -1
          assembler = Assembler()
          connection_started = now
      if transport is not None:
        if now - last_heartbeat >= .5:
          yolo = read_fresh(RUNTIME / 'yolo.json', 2) or {}
          data = {'role': 'jetson', 'session': session, 'video': args.video,
                  'hud_connected': read_fresh(RUNTIME / 'hud-status.json', 2) is not None,
                  'temperature_c': temperature(), 'ip': local_ip(args.comma),
                  'yolo_recent': bool(yolo)}
          sequence += 1
          transport.send(Msg.HEARTBEAT, sequence, [json.dumps(data, allow_nan=False).encode()], timeout=.5)
          last_heartbeat = now
        try:
          message = transport.recv(.02)
        except LinkTimeout:
          message = None
        if message is not None:
          raw = bytes(message.payload)
          now = time.monotonic()
          if message.msg_type == Msg.HEARTBEAT:
            peer.accept(raw, message.seq, now)
          elif peer.alive(now) and message.seq > last_data_sequence:
            last_data_sequence = message.seq
            raw = assembler.feed(message.msg_type, raw, now)
            if raw is None:
              continue
            if message.msg_type == Msg.HUD:
              from openpilot.tools.jetson.snapshot import validate_snapshot
              value = decode_json(raw, 512 * 1024)
              validate_snapshot(value)
              value.update(updated=now, session=peer.session)
              atomic_json(SNAPSHOT, value)
            elif message.msg_type == Msg.ROAD_VIDEO:
              video.select(True)
              video.publish(raw)
            elif message.msg_type == Msg.NAVI_MEDIA:
              if messaging.log_from_bytes(raw).which() != 'carrotNaviMedia':
                raise ValueError('wrong navigation media service')
              navi.send(raw)
        if now - connection_started > 2 and not peer.alive(now):
          raise LinkError('Comma heartbeat expired')
        retry.observe(peer.alive(now), now)
    except Exception as exc:
      log.warning('USB display link unavailable: %s', exc)
      if transport is not None:
        transport.close()
      transport = None
      peer = Peer('comma')
      SNAPSHOT.unlink(missing_ok=True)
      retry.failed(time.monotonic())
      next_connect = retry.next_attempt
    now = time.monotonic()
    video.select(peer.alive(now))
    video.poll_wifi()
    if now - last_status >= .25:
      status = display_status(peer, now)
      # Host status describes the local Jetson, while peer liveness is measured
      # on this machine's monotonic clock. A TCP socket alone is not readiness.
      status.update(ip=local_ip(args.comma), temperature_c=temperature(),
                    comma_ip=args.comma, video_source=video.source, video_epoch=video.epoch,
                    transport='usb' if peer.alive(now) else 'wifi',
                    hud_connected=read_fresh(RUNTIME / 'hud-status.json', 2) is not None,
                    yolo_recent=read_fresh(RUNTIME / 'yolo.json', 2) is not None,
                    comma_tcp=video.source == 'wifi' and video.last_received is not None
                    and now - video.last_received < 2)
      atomic_json(STATUS, status)
      packet = json.dumps(status, allow_nan=False).encode()
      for port in (8766, 8767, 8768):
        try:
          udp.sendto(packet, ('127.0.0.1', port))
          if args.comma and not peer.alive(now):
            udp.sendto(packet, (args.comma, port))
        except OSError:
          pass
      last_status = now
    time.sleep(.005)


if __name__ == '__main__':
  main()
