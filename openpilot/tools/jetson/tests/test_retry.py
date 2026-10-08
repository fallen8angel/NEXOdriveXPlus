"""Bounded retry and real supervisor-loop tests with USB/socket I/O replaced."""
import json
from types import SimpleNamespace
import sys

import pytest

from openpilot.cereal import log
from openpilot.tools.jetson import host, state, vehicle
from openpilot.tools.jetson.fragments import fragments
from openpilot.tools.jetson.retry import UsbRetry
from openpilot.tools.jetson.transport.base import LinkError, Message
from openpilot.tools.jetson.transport.protocol import Msg


def test_failures_survive_service_restart_but_not_reboot(tmp_path):
  path = tmp_path / 'retry.json'
  for attempt, now in enumerate((10, 11, 13), start=1):
    retry = UsbRetry(path, 'boot-a')
    assert retry.ready(now)
    retry.begin(now)
    retry.failed(now)
    assert retry.failures == attempt
    assert not retry.ready(now + .99)
  retry = UsbRetry(path, 'boot-a')
  assert not retry.ready(10000)
  with pytest.raises(RuntimeError):
    retry.begin(10000)
  reboot = UsbRetry(path, 'boot-b')
  assert reboot.ready(0)
  reboot.begin(0)
  assert reboot.failures == 1
  assert json.loads(path.read_text())['boot_id'] == 'boot-b'


def test_killed_attempt_is_counted_before_open(tmp_path):
  path = tmp_path / 'retry.json'
  for now in (10, 11, 13):
    UsbRetry(path, 'same-boot').begin(now)
  assert not UsbRetry(path, 'same-boot').ready(1000)


def test_only_stable_health_resets_budget(tmp_path):
  path = tmp_path / 'retry.json'
  retry = UsbRetry(path, 'boot')
  retry.begin(10)
  retry.observe(True, 10)
  retry.observe(True, 11.99)
  assert UsbRetry(path, 'boot').failures == 1
  retry.observe(False, 12)  # stale data breaks confirmation
  retry.observe(True, 20)
  assert retry.failures == 1
  retry.observe(True, 22)
  assert UsbRetry(path, 'boot').failures == 0
  retry.failed(23)
  assert not retry.ready(23)
  assert retry.ready(24)
  retry.begin(24)
  assert retry.failures == 2  # two consecutive failures since last stable connection


@pytest.mark.parametrize('value', [
  {'failures': -1, 'next_attempt': 0}, {'failures': True, 'next_attempt': 0},
  {'failures': 0, 'next_attempt': -1}, {'failures': 4, 'next_attempt': 0},
])
def test_invalid_same_boot_state_does_not_grant_attempts(tmp_path, value):
  path = tmp_path / 'retry.json'
  path.write_text(json.dumps(value | {'boot_id': 'boot'}))
  assert not UsbRetry(path, 'boot').ready(100)
  assert UsbRetry(path, 'next-boot').ready(0)


class StopLoop(BaseException):
  pass


class Clock:
  def __init__(self):
    self.now = 10.
    self.until = 30.

  def advance(self, seconds=.25):
    self.now += seconds
    if self.now >= self.until:
      raise StopLoop

  def sleep(self, seconds):
    self.advance(max(.25, seconds))


def encoded(number=1):
  event = log.Event.new_message()
  video = event.init('roadEncodeData')
  video.idx.encodeId, video.idx.flags = number, 8
  video.header, video.data = b'header', b'compressed'
  return event.to_bytes()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
  import openpilot.cereal as cereal
  clock = Clock()
  publications, subscriptions = {}, []
  def subscribe(name, **kwargs):
    subscriptions.append((name, kwargs))
    queue = [encoded()]
    return SimpleNamespace(receive=lambda **kwargs: queue.pop() if queue else None)

  def decode(raw):
    with log.Event.from_bytes(raw) as event:
      return event.as_builder()

  messaging = SimpleNamespace(
    reset_context=lambda: None,
    pub_sock=lambda name: SimpleNamespace(send=publications.setdefault(name, []).append),
    sub_sock=subscribe, log_from_bytes=decode,
  )
  monkeypatch.setitem(sys.modules, 'openpilot.cereal.messaging', messaging)
  monkeypatch.setattr(cereal, 'messaging', messaging, raising=False)
  monkeypatch.setattr(host.time, 'monotonic', lambda: clock.now)
  monkeypatch.setattr(host.time, 'sleep', clock.sleep)
  monkeypatch.setattr(host.os, 'nice', lambda value: None, raising=False)
  monkeypatch.setattr(host.os, 'sched_setaffinity', lambda *args: None, raising=False)
  clock.sockets = []
  def make_socket(*args):
    sock = SimpleNamespace(setblocking=lambda flag: None, sendto=lambda *args: None, closed=0)
    def close():
      sock.closed += 1
    sock.close = close
    clock.sockets.append(sock)
    return sock
  monkeypatch.setattr(host.socket, 'socket', make_socket)
  monkeypatch.setattr(host, 'temperature', lambda: None)
  monkeypatch.setattr(host, 'local_ip', lambda comma: '')
  for module in (host, state):
    monkeypatch.setattr(module, 'RUNTIME', tmp_path)
  for module in (host, vehicle):
    monkeypatch.setattr(module, 'UsbRetry', lambda path: UsbRetry(path, 'test-boot'))
  monkeypatch.setattr(host, 'STATUS', tmp_path / 'status.json')
  monkeypatch.setattr(host, 'SNAPSHOT', tmp_path / 'hud.json')
  return clock, publications, subscriptions


@pytest.mark.parametrize('present', [False, True])
def test_host_absence_or_io_failure_is_bounded_and_keeps_wifi(monkeypatch, tmp_path, runtime, present):
  from openpilot.tools.jetson.transport.usbbulk import UsbBulkTransport
  clock, publications, subscriptions = runtime
  attempts, closed = [], []
  class Broken:
    def send(self, *args, **kwargs):
      raise LinkError('bulk OUT 0x02 failed: Input/Output Error')

    def close(self):
      closed.append(True)

  def open_link():
    attempts.append(clock.now)
    return Broken()

  monkeypatch.setattr(UsbBulkTransport, 'present', lambda: present)
  monkeypatch.setattr(UsbBulkTransport, 'open', open_link)
  monkeypatch.setattr(sys, 'argv', ['host', '--comma', '192.0.2.1', '--video'])
  with pytest.raises(StopLoop):
    host.main()
  assert len(attempts) == len(closed) == (3 if present else 0)
  assert len(subscriptions) == 1  # fallback source remains subscribed
  assert len(publications['roadEncodeData']) == 1
  assert set(publications) == {'roadEncodeData', 'carrotNaviMedia'}
  assert not (tmp_path / 'hud.json').exists()
  if present:
    assert not UsbRetry(tmp_path / 'host-usb-retry.json', 'test-boot').ready(100)
    assert UsbRetry(tmp_path / 'host-usb-retry.json', 'next-boot').ready(0)
    assert attempts[1] - attempts[0] >= 1
    assert attempts[2] - attempts[1] >= 2
  else:
    assert not (tmp_path / 'host-usb-retry.json').exists()


def test_host_reconnect_restores_snapshot_video_and_health(monkeypatch, tmp_path, runtime):
  from openpilot.tools.jetson.transport.usbbulk import UsbBulkTransport
  clock, publications, _ = runtime
  opened = []
  class Link:
    def __init__(self, fail):
      self.fail, self.sequence = fail, 0

    def send(self, *args, **kwargs):
      if self.fail:
        raise LinkError('usb bulk write failed: I/O error')

    def recv(self, timeout):
      self.sequence += 1
      kind = (Msg.HEARTBEAT, Msg.HUD, Msg.ROAD_VIDEO)[(self.sequence - 1) % 3]
      if kind == Msg.HEARTBEAT:
        raw = json.dumps({'role': 'comma', 'session': 'a' * 32}).encode()
      elif kind == Msg.HUD:
        value = {'version': 1, 'sent': clock.now}
        value.update({key: {} for key in ('params', 'events', 'mono', 'received', 'valid', 'alive', 'cameras')})
        raw = json.dumps(value).encode()
      else:
        raw = encoded(self.sequence)
      if kind != Msg.HEARTBEAT:
        raw = next(iter(fragments(raw, self.sequence)))
      return Message(kind, self.sequence, 0, memoryview(raw))

    def close(self):
      pass

  def open_link():
    link = Link(fail=not opened)
    opened.append(link)
    return link

  monkeypatch.setattr(UsbBulkTransport, 'present', lambda: True)
  monkeypatch.setattr(UsbBulkTransport, 'open', open_link)
  monkeypatch.setattr(sys, 'argv', ['host', '--video'])
  with pytest.raises(StopLoop):
    host.main()
  assert len(opened) == 2
  assert json.loads((tmp_path / 'hud.json').read_text())['session'] == 'a' * 32
  assert publications['roadEncodeData']
  assert json.loads((tmp_path / 'video-input.json').read_text())['source'] == 'usb'
  assert UsbRetry(tmp_path / 'host-usb-retry.json', 'test-boot').failures == 0


@pytest.mark.parametrize('attached', [False, True])
def test_vehicle_absence_or_io_failure_is_bounded(monkeypatch, tmp_path, runtime, attached):
  from openpilot.tools.jetson.transport.ffs import FfsTransport
  clock, _, _ = runtime
  opened, closed, statuses = [], [], []
  locks = []
  original_open = type(tmp_path).open
  def open_path(path, *args, **kwargs):
    handle = original_open(path, *args, **kwargs)
    if path.name == 'vehicle.lock':
      locks.append(handle)
    return handle
  monkeypatch.setattr(type(tmp_path), 'open', open_path)
  monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=lambda *args: None, LOCK_EX=1, LOCK_NB=2))
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', SimpleNamespace(Params=lambda: SimpleNamespace(get_bool=lambda key: True)))
  monkeypatch.setattr(vehicle, 'host_attached', lambda: attached)
  monkeypatch.setattr(vehicle, 'local_status', lambda peer, sock: statuses.append(peer))
  monkeypatch.setattr(vehicle, 'Publisher', lambda: SimpleNamespace(close=lambda: None))
  monkeypatch.setattr(vehicle, 'MOUNT', str(tmp_path))
  (tmp_path / 'ep0').touch()

  def initialize(self, *args, **kwargs):
    opened.append(clock.now)

  def fail_send(self, *args, **kwargs):
    raise LinkError('gadget write had no reader before deadline; link abandoned')

  monkeypatch.setattr(FfsTransport, '__init__', initialize)
  monkeypatch.setattr(FfsTransport, 'send', fail_send)
  monkeypatch.setattr(FfsTransport, 'close', lambda self: closed.append(True))
  with pytest.raises(StopLoop):
    vehicle.main()
  assert len(locks) == 1 and locks[0].closed
  assert clock.sockets[0].closed == 1
  assert len(opened) == len(closed) == (3 if attached else 0)
  assert not statuses[-1].alive(clock.now)
  if attached:
    assert not UsbRetry(tmp_path / 'vehicle-usb-retry.json', 'test-boot').ready(100)
    assert UsbRetry(tmp_path / 'vehicle-usb-retry.json', 'next-boot').ready(0)
  else:
    assert not (tmp_path / 'vehicle-usb-retry.json').exists()


def test_usb_bulk_io_error_is_link_failure_not_model_rejection(monkeypatch):
  from openpilot.tools.jetson.transport.usbbulk import UsbBulkTransport
  class USBError(Exception):
    pass
  class USBErrorTimeout(USBError):
    pass
  monkeypatch.setitem(sys.modules, 'usb1', SimpleNamespace(USBError=USBError, USBErrorTimeout=USBErrorTimeout))
  def fail(*args, **kwargs):
    raise USBError('Input/Output Error')
  transport = UsbBulkTransport(SimpleNamespace(bulkWrite=fail))
  with pytest.raises(LinkError, match='usb bulk write failed'):
    transport.send(Msg.HEARTBEAT, 1, [b'heartbeat'], timeout=.5)
  # The failed stream is discarded; a freshly opened stream has no permanent rejection.
  writes = []
  fresh = UsbBulkTransport(SimpleNamespace(bulkWrite=lambda endpoint, data, **kwargs: writes.append(bytes(data)) or len(data)))
  fresh.send(Msg.HEARTBEAT, 1, [b'heartbeat'], timeout=.5)
  assert writes


def test_vehicle_reconnect_confirms_successful_hud_transfer(monkeypatch, tmp_path, runtime):
  from openpilot.tools.jetson.transport.ffs import FfsTransport
  clock, _, _ = runtime
  opened, sent, closed = [], [], []
  monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=lambda *args: None, LOCK_EX=1, LOCK_NB=2))
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', SimpleNamespace(Params=lambda: SimpleNamespace(get_bool=lambda key: True)))
  monkeypatch.setattr(vehicle, 'host_attached', lambda: True)
  monkeypatch.setattr(vehicle, 'local_status', lambda *args: None)
  monkeypatch.setattr(vehicle, 'Publisher', lambda: SimpleNamespace(snapshot=lambda flag: b'{}', media=lambda flag: [], close=lambda: None))
  monkeypatch.setattr(vehicle, 'MOUNT', str(tmp_path))
  (tmp_path / 'ep0').touch()

  def initialize(self, *args, **kwargs):
    self.fail, self.sequence = not opened, 0
    opened.append(clock.now)

  def send(self, kind, *args, **kwargs):
    if self.fail:
      raise LinkError('gadget write failed: I/O error')
    sent.append(kind)

  def receive(self, timeout):
    clock.advance()
    self.sequence += 1
    raw = json.dumps({'role': 'jetson', 'session': 'a' * 32, 'hud_connected': True, 'video': False}).encode()
    return Message(Msg.HEARTBEAT, self.sequence, 0, memoryview(raw))

  monkeypatch.setattr(FfsTransport, '__init__', initialize)
  monkeypatch.setattr(FfsTransport, 'send', send)
  monkeypatch.setattr(FfsTransport, 'recv', receive)
  monkeypatch.setattr(FfsTransport, 'close', lambda self: closed.append(True))
  with pytest.raises(StopLoop):
    vehicle.main()
  assert clock.sockets[0].closed == 1
  assert len(opened) == len(closed) == 2
  assert Msg.HUD in sent and Msg.HEARTBEAT in sent
  assert UsbRetry(tmp_path / 'vehicle-usb-retry.json', 'test-boot').failures == 0
