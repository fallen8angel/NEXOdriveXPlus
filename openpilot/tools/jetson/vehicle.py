"""Optional USB display owner, isolated from controls/modeld and disabled by default."""
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

from openpilot.tools.jetson.state import Peer, STATUS, atomic_json, display_status
from openpilot.tools.jetson.fragments import fragments
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout
from openpilot.tools.jetson.transport.protocol import Msg
from openpilot.tools.jetson.retry import UsbRetry

GADGET = '/sys/kernel/config/usb_gadget/nexo-display'
MOUNT = '/dev/ffs-nexo-display'
ROLE = Path('/sys/class/power_supply/usb/typec_mode')
log = logging.getLogger(__name__)


def host_attached():
  try:
    return ROLE.read_text().strip().startswith('Source attached')
  except OSError:
    return False


def local_status(peer, sock, tx=None, error=''):
  value = display_status(peer, time.monotonic())
  value.update(tx or {})
  if error:
    from openpilot.tools.jetson.state import public_text
    value['last_error'] = public_text(error)
  try:
    atomic_json(STATUS, value)
  except OSError:
    pass
  packet = json.dumps(value, allow_nan=False).encode()
  for port in (8766, 8767, 8768):
    try:
      sock.sendto(packet, ('127.0.0.1', port))
    except OSError:
      pass


def session(transport, should_run, publisher, sock, on_health=None, initial_message=None):
  peer = Peer('jetson')
  if initial_message is not None:
    peer.accept(bytes(initial_message.payload), initial_message.seq, time.monotonic())
  nonce = uuid.uuid4().hex
  sequence = 0
  last_heartbeat = last_hud = 0.
  tx = {}
  started = time.monotonic()

  def send(kind, raw):
    nonlocal sequence
    packets = (raw,) if kind == Msg.HEARTBEAT else fragments(raw, sequence + 1)
    deadline = time.monotonic() + .5
    for packet in packets:
      sequence += 1
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise LinkError('display record write deadline exceeded')
      transport.send(kind, sequence, [packet], timeout=remaining)

  try:
    while should_run():
      now = time.monotonic()
      if now - last_heartbeat >= .5:
        send(Msg.HEARTBEAT, json.dumps({'role': 'comma', 'session': nonce}).encode())
        last_heartbeat = now
        local_status(peer, sock, tx)
      try:
        message = transport.recv(.01)
        # The host may only send telemetry. It cannot publish cereal, change
        # Params, request CAN writes, update software or affect control state.
        if message.msg_type != Msg.HEARTBEAT:
          raise LinkError('unexpected host message')
        peer.accept(bytes(message.payload), message.seq, time.monotonic())
      except LinkTimeout:
        pass
      now = time.monotonic()
      if now - started > 2 and not peer.alive(now):
        raise LinkError('Jetson heartbeat expired')
      if not peer.alive(now):
        if on_health is not None:
          on_health(False, now)
        continue
      if now - last_hud >= .1:
        packet = publisher.snapshot(peer.value.get('hud_connected') is True)
        if packet:
          send(Msg.HUD, packet)
          tx['last_hud_tx_mono'] = time.monotonic()
          if getattr(publisher, 'camera_frame_mono', None) is not None:
            tx['last_camera_tx_mono'] = tx['last_hud_tx_mono']
            tx['camera_frame_mono'] = publisher.camera_frame_mono
        last_hud = now
      for kind, raw in publisher.media(peer.value.get('video') is True):
        send(kind, raw)
      if on_health is not None:
        on_health(True, time.monotonic())
  finally:
    local_status(Peer('jetson'), sock)


class Publisher:
  def __init__(self):
    from openpilot.cereal import messaging
    from openpilot.tools.jetson.snapshot import SnapshotBuilder
    self.messaging = messaging
    self.builder = SnapshotBuilder()
    self.road = None
    self.navi = messaging.sub_sock('carrotNaviMedia', conflate=False)
    self.camera = None
    self.hud_connected = False
    self.camera_profile = 'legacy'
    self.camera_frame_mono = None
    self.next_camera_attempt = 0.

  def snapshot(self, hud_connected):
    from openpilot.tools.jetson.camera import CameraPublisher
    enabled = hud_connected and (self.builder.params.get_bool('IsOnroad') or self.builder.params.get_int('ClusterHudDebug') > 0)
    if enabled and not self.hud_connected:
      # Reuse the existing display-media bootstrap request. This asks the
      # navigation publisher to replay cached images/config; no navigation or
      # speed calculation is changed and nothing is accepted as a remote key.
      self.builder.params.put_nonblocking('CarrotNaviWebBootstrapRequest', f'nexo-usb:{uuid.uuid4().hex}')
    self.hud_connected = enabled
    if self.camera is not None and self.camera.process.poll() is not None:
      try:
        self.camera.close()
      except OSError:
        pass
      self.camera = None
    if enabled and self.camera is None and time.monotonic() >= self.next_camera_attempt:
      self.next_camera_attempt = time.monotonic() + 5
      try:
        self.camera = CameraPublisher(self.camera_profile)
      except OSError:
        log.exception('optional camera preview worker unavailable')
    elif not enabled and self.camera is not None:
      self.camera.close()
      self.camera = None
    try:
      cameras = self.camera.latest if self.camera else {}
      self.camera_frame_mono = max((frame['time'] for frame in cameras.values()), default=None)
      return self.builder.packet(cameras)
    except Exception:
      log.exception('HUD snapshot unavailable')
      return None

  @property
  def metrics(self):
    return self.camera.health if self.camera else {}

  def media(self, video):
    if not video or not self.builder.params.get_bool('IsOnroad'):
      self.road = None
    elif self.road is None:
      self.road = self.messaging.sub_sock('roadEncodeData', conflate=False)
    # Bound each turn; no whole-stream drains, model decoding or inference.
    for kind, sock, count in ((Msg.ROAD_VIDEO, self.road, 2), (Msg.NAVI_MEDIA, self.navi, 2)):
      if sock is None:
        continue
      for _ in range(count):
        event = self.messaging.recv_one_or_none(sock)
        if event is None:
          break
        if kind == Msg.NAVI_MEDIA and event.carrotNaviMedia.kind in ('web_render', 'web_image'):
          event = event.as_builder()
          event.carrotNaviMedia.kind = 'render' if event.carrotNaviMedia.kind == 'web_render' else 'image'
        raw = event.to_bytes() if hasattr(event, 'to_bytes') else event.as_builder().to_bytes()
        if len(raw) <= 1024 * 1024 and 0 <= time.monotonic() - event.logMonoTime / 1e9 < .5:
          yield kind, raw

  def close(self):
    if self.camera is not None:
      self.camera.close()


def main():
  import fcntl
  from openpilot.common.params import Params
  from openpilot.tools.jetson.state import RUNTIME
  params = Params()
  RUNTIME.mkdir(parents=True, exist_ok=True)
  lock = (RUNTIME / 'vehicle.lock').open('w')
  try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # No realtime priority or model/DM CPU affinity changes.
    os.nice(10)
    try:
      os.sched_setaffinity(0, set(range(min(4, os.cpu_count() or 1))))
    except OSError:
      pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
      sock.setblocking(False)
      retry = UsbRetry(RUNTIME / 'vehicle-usb-retry.json')
      retry.recover_after = 30.
      run(params, sock, retry)
    finally:
      sock.close()
  finally:
    lock.close()


def run(params, sock, retry):
  from openpilot.tools.jetson.transport.ffs import FfsTransport
  last_error = ''
  while params.get_bool('NexoJetsonUsb'):
    transport = publisher = None
    try:
      # The native inference owner already speaks JLNK. Never contend for its UDC.
      if Path('/data/nexo_jetlink_enabled').exists():
        local_status(Peer('jetson'), sock)
        time.sleep(1)
        continue
      if not host_attached():
        local_status(Peer('jetson'), sock, error=last_error)
        time.sleep(1)
        continue
      if not retry.ready(time.monotonic()):
        local_status(Peer('jetson'), sock, error=last_error)
        time.sleep(1)
        continue
      retry.begin(time.monotonic())
      # Root setup touches only this gadget. It refuses an occupied UDC and
      # has a deadline; failure stays in this optional process's retry loop.
      if not Path(MOUNT, 'ep0').exists():
        subprocess.run(['sudo', '-n', 'bash', str(Path(__file__).with_name('setup_gadget.sh'))],
                       check=True, timeout=10, stdin=subprocess.DEVNULL)
      transport = FfsTransport(MOUNT, gadget=GADGET)
      publisher = Publisher()
      from openpilot.tools.jetson import carrot
      mode, initial = carrot.select(transport)
      last_error = ''
      publisher.camera_profile = 'carrot' if mode == 'carrot' else 'legacy'
      def should_run():
        return host_attached() and params.get_bool('NexoJetsonUsb') and not Path('/data/nexo_jetlink_enabled').exists()
      if mode == 'carrot':
        carrot.session(transport, should_run, publisher, sock, on_health=retry.observe)
      else:
        session(transport, should_run, publisher, sock, on_health=retry.observe, initial_message=initial)
    except Exception as exc:
      log.warning('optional Jetson display disconnected: %s', exc)
      retry.failed(time.monotonic())
      last_error = str(exc)
    finally:
      if transport is not None:
        transport.close()
      if publisher is not None:
        publisher.close()
      local_status(Peer('jetson'), sock, error=last_error)
    time.sleep(1)


if __name__ == '__main__':
  main()
