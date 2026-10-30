"""Optional NEXO display packets on the inference owner's negotiated USB link.

As in Carrot, snapshot/preview and navigation work run in fresh, low-priority
children. Only the inference owner writes USB, after replying to modeld. A
failed producer expires its data; a USB write failure still invalidates the
shared transport rather than continuing on a partially written stream.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import uuid

from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.tools.jetson.state import RUNTIME
from openpilot.tools.jetson.process import stop_process

CAPABILITY = 'nexo_hud_v1'
HUD_LIMIT = 96 * 1024
HEADER = struct.Struct('<d')
MEDIA_HEADER = struct.Struct('<QQII')
CHUNK = 32 * 1024
MAX_MEDIA = 1024 * 1024
TAIL_BUDGET = .012


def snapshot_packet(raw: bytes, session: str) -> bytes:
  from openpilot.tools.jetson.snapshot import MAX_SNAPSHOT, validate_snapshot
  from openpilot.tools.jetson.state import decode_json
  value = validate_snapshot(decode_json(raw, MAX_SNAPSHOT))
  value['session'] = session
  # A large legacy inline guidance image must not suppress speed/state or
  # camera updates. Navigation media has its independent ordered channel.
  if len(value['params'].get('CarrotNaviImage', '')) > 16 * 1024:
    value['params'].pop('CarrotNaviImage', None)
  # Retain NEXO parking, settings and side/reverse previews. Large routes and
  # supplementary model diagnostics yield to the bounded display allocation.
  discard = ['navRoute', 'liveTracks', 'drivingModelData', 'cameraOdometry',
             'liveDelay', 'liveParameters', 'liveTorqueParameters', 'livePose',
             'lateralPlan', 'longitudinalPlan', 'wideRoadCameraState']
  while True:
    packet = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(packet) <= HUD_LIMIT:
      return packet
    # Keep compact optical HUD observations when large navigation/model
    # diagnostics can be discarded first. Never displace the core car state.
    # LiveTorqueParameters is a persisted Params blob, not the compact
    # liveTorqueParameters event. It can exceed 250 KiB after calibration;
    # omit it only when over budget, before sacrificing useful HUD events.
    if 'LiveTorqueParameters' in value['params']:
      value['params'].pop('LiveTorqueParameters')
      continue
    if discard:
      name = discard.pop(0)
      for key in ('events', 'mono', 'received', 'valid', 'alive'):
        value[key].pop(name, None)
    elif 'signal_display' in value:
      value.pop('signal_display')
    elif value['cameras']:
      for name in ('wide', 'road', 'driver'):
        if name in value['cameras']:
          del value['cameras'][name]
          break
      else:
        value['cameras'].popitem()
    else:
      raise ValueError('NEXO HUD core snapshot exceeds allocation')


def fragments(raw, epoch, sequence):
  if not 0 < len(raw) <= MAX_MEDIA:
    return
  for offset in range(0, len(raw), CHUNK):
    yield MEDIA_HEADER.pack(epoch, sequence, offset, len(raw)) + raw[offset:offset + CHUNK]


def send_event(sock, raw, epoch, sequence):
  if not 0 < len(raw) <= MAX_MEDIA:
    return False
  deadline = time.monotonic() + 1.8
  for fragment in fragments(raw, epoch, sequence):
    while True:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        return False
      sock.settimeout(min(.1, remaining))
      try:
        if sock.send(fragment) != len(fragment):
          raise OSError('partial display SEQPACKET write')
        break
      except TimeoutError:
        continue
  return True


class DisplayPublisher:
  def __init__(self, navi=True, standalone=False):
    self.standalone = standalone
    RUNTIME.mkdir(parents=True, exist_ok=True)
    self.path = RUNTIME / f'native-display-{os.getpid()}.packet'
    self.last_sent = 0.
    self.next_hud = 0.
    self.hud_connected = None
    self.worker_attempts = 0
    self.worker_retry_at = 0.
    self.worker_healthy_since = None
    self.media_socket = self.media_process = self.process = None
    self.status = {'capability_negotiated': True, 'display_error': '', 'hud_enabled': True}
    try:
      self.process = subprocess.Popen(
        [sys.executable, '-m', __name__, 'snapshot', str(self.path), str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
      if navi:
        self.media_socket, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.media_socket.setblocking(False)
        try:
          self.media_process = subprocess.Popen(
            [sys.executable, '-m', __name__, 'media', str(child.fileno()), str(os.getpid())],
            pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        finally:
          child.close()
    except Exception:
      self.close()
      raise

  def packet(self, now):
    if self.process.poll() is not None:
      self.status['display_error'] = 'HUD snapshot worker stopped · inference continues'
      return None
    try:
      with self.path.open('rb') as src:
        raw = src.read(HUD_LIMIT + HEADER.size + 1)
      if not HEADER.size < len(raw) <= HUD_LIMIT + HEADER.size:
        return None
      stamp, = HEADER.unpack_from(raw)
      if stamp == self.last_sent or not 0 <= now - stamp < .3:
        self.status['snapshot_drop_age_s'] = max(0., now - stamp)
        return None
      self.last_sent = stamp
      self.status['hud_produced_mono'] = stamp
      self.status['display_error'] = ''
      return raw[HEADER.size:]
    except (OSError, struct.error):
      return None

  def media_packet(self):
    if self.media_socket is not None:
      try:
        raw = self.media_socket.recv(CHUNK + MEDIA_HEADER.size + 1)
        if MEDIA_HEADER.size < len(raw) <= CHUNK + MEDIA_HEADER.size:
          return raw
      except (BlockingIOError, OSError):
        pass
    return None

  def send(self, client, clock=time.monotonic):
    """Called only with no outstanding inference reply; never wait for workers."""
    started = clock()
    deadline = started + TAIL_BUDGET
    if started >= self.next_hud:
      self.next_hud = started + .1
      state = client.last_state if isinstance(client.last_state, dict) else {}
      telemetry = state.get('telemetry', state)
      connected = isinstance(telemetry, dict) and telemetry.get('carrot_hud_connected') is True
      if connected != self.hud_connected:
        try:
          path = self.path.with_suffix('.connected')
          temporary = path.with_name(path.name + '.tmp')
          temporary.write_bytes(b'1' if connected else b'0')
          os.replace(temporary, path)
          self.hud_connected = connected
        except OSError:
          pass  # Only the display worker reads this marker, never controls.
      raw = self.packet(started)
      prepared = clock()
      self.status['hud_prepare_ms'] = (prepared - started) * 1000
      # Only the separate Display Only process may prepare outside the wire
      # budget. The inference owner's total 12 ms tail remains unchanged.
      if getattr(self, 'standalone', False):
        deadline = prepared + TAIL_BUDGET
        if prepared - self.last_sent >= .3:
          raw = None
      remaining = deadline - clock()
      if raw and remaining > 0:
        sending = clock()
        try:
          client.t.send(P.Msg.HUD, client._next_seq(), [raw], timeout=remaining)
        finally:
          self.status.update(hud_send_ms=(clock() - sending) * 1000, hud_send_budget_ms=remaining * 1000,
                             hud_source_age_s=max(0., sending - self.last_sent))
        self.status['hud_bytes'] = len(raw)
        if not getattr(client.t, 'deferred', False):
          self.status['last_hud_tx_mono'] = clock()
        else:
          self.status['last_hud_queued_mono'] = clock()
    for _ in range(2):
      if clock() >= deadline or (getattr(self, 'standalone', False) and deadline - clock() < .004):
        break
      packet = self.media_packet()
      if not packet:
        break
      remaining = deadline - clock()
      if remaining <= 0:
        break
      client.t.send(P.Msg.NAVI_MEDIA, client._next_seq(), [packet], timeout=remaining)
      self.status['last_navi_tx_mono'] = clock()

  def recover_snapshot(self, now):
    """Only the optional NCM owner calls this; never restart the transport/GPU."""
    if self.process.poll() is None:
      stamp = self.status.get('hud_produced_mono')
      if stamp is not None and 0 <= now - stamp < .3:
        self.worker_healthy_since = self.worker_healthy_since or now
        if now - self.worker_healthy_since >= 30:
          self.worker_attempts = 0
      else:
        self.worker_healthy_since = None
      return
    self.worker_healthy_since = None
    # Do not permanently disable the HUD snapshot producer after three
    # crashes. Bound retries to at most one per minute during persistent
    # failure; inference and the USB owner remain untouched.
    if now < self.worker_retry_at:
      return
    self.worker_attempts += 1
    self.worker_retry_at = now + min(60, 2 ** min(self.worker_attempts, 6))
    self.status['snapshot_restart_attempts'] = self.worker_attempts
    try:
      self.path.unlink(missing_ok=True)
      self.process = subprocess.Popen(
        [sys.executable, '-m', __name__, 'snapshot', str(self.path), str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
      self.last_sent = 0.
    except OSError:
      self.status['display_error'] = 'HUD snapshot restart failed'

  def close(self):
    for process in (self.media_process, self.process):
      if process is not None:
        stop_process(process)
    if self.media_socket is not None:
      self.media_socket.close()
    for path in (self.path, self.path.with_suffix('.tmp'), self.path.with_suffix('.connected'),
                 self.path.with_suffix('.connected.tmp')):
      try:
        path.unlink(missing_ok=True)
      except OSError:
        pass


def worker(mode, argument, parent):
  import ctypes
  import signal
  ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
  if os.getppid() != parent:
    return

  def stop(*_):
    raise SystemExit

  signal.signal(signal.SIGTERM, stop)
  os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  os.nice(19)
  os.sched_setaffinity(0, set(range(min(4, os.cpu_count() or 1))))
  if mode == 'media':
    from openpilot.cereal import messaging
    source = messaging.sub_sock('carrotNaviMedia', conflate=False, timeout=100)
    with socket.socket(fileno=int(argument)) as sock:
      epoch, sequence = time.monotonic_ns(), 0
      while os.getppid() == parent:
        event = messaging.recv_one(source)
        if event is None or not 0 <= time.monotonic_ns() - event.logMonoTime < 500_000_000:
          continue
        event = event.as_builder()
        if event.carrotNaviMedia.kind in ('web_render', 'web_image'):
          event.carrotNaviMedia.kind = 'render' if event.carrotNaviMedia.kind == 'web_render' else 'image'
        sequence += 1
        send_event(sock, event.to_bytes(), epoch, sequence)
    return

  from openpilot.tools.jetson.vehicle import Publisher
  publisher = Publisher()
  path = Path(argument)
  session = uuid.uuid4().hex
  try:
    while os.getppid() == parent:
      started = time.monotonic()
      try:
        try:
          hud_connected = path.with_suffix('.connected').read_bytes() == b'1'
        except OSError:
          hud_connected = False
        raw = publisher.snapshot(hud_connected)
        if raw:
          packet = snapshot_packet(raw, session)
          temporary = path.with_suffix('.tmp')
          temporary.write_bytes(HEADER.pack(started) + packet)
          os.replace(temporary, path)
      except (OSError, ValueError, TypeError, RecursionError):
        pass  # Old packets expire without disturbing modeld.
      time.sleep(max(0., started + .1 - time.monotonic()))
  finally:
    publisher.close()
    path.unlink(missing_ok=True)
    path.with_suffix('.tmp').unlink(missing_ok=True)


if __name__ == '__main__':
  worker(sys.argv[1], sys.argv[2], int(sys.argv[3]))
