import struct
import threading
import time

import pytest

from openpilot.tools.jetson.transport import protocol as P
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout, MAX_MESSAGE, StreamTransport
from openpilot.tools.jetson.transport.ffs import build_descriptors
from openpilot.tools.jetson.transport.watchdog import WriteWatchdog
from openpilot.tools.jetson.state import Peer, VideoGate, atomic_json, display_status, read_fresh
from openpilot.tools.jetson.fragments import Assembler, fragments, CHUNK, MAX_RECORD


class MemoryTransport(StreamTransport):
  def __init__(self, data=b'', chunk=7):
    super().__init__()
    self.input = bytearray(data)
    self.output = bytearray()
    self.chunk = chunk

  def _write(self, bufs):
    raw = b''.join(bufs)[:self.chunk]
    self.output.extend(raw)
    return len(raw)

  def _read_into(self, dest, timeout):
    size = min(len(dest), len(self.input), self.chunk)
    dest[:size] = self.input[:size]
    del self.input[:size]
    if not size:
      time.sleep(min(timeout or .001, .001))
    return size

  def close(self):
    pass


@pytest.mark.parametrize('size', [0, 1, 992, 1024, 16352, 50000])
def test_partial_io_and_padding(size):
  sender = MemoryTransport()
  sender.send(P.Msg.HUD, 4, [b'x' * size], timeout=2)
  receiver = MemoryTransport(sender.output)
  message = receiver.recv(2)
  assert (message.msg_type, message.seq, bytes(message.payload)) == (P.Msg.HUD, 4, b'x' * size)
  assert not receiver.input


def test_partial_timeout_keeps_framing():
  raw = P.pack_header(P.Msg.HUD, 2, 5) + b'hello'
  receiver = MemoryTransport(raw[:34])
  with pytest.raises(LinkTimeout):
    receiver.recv(.01)
  receiver.input.extend(raw[34:])
  assert bytes(receiver.recv(.1).payload) == b'hello'


def test_reject_inference_unknown_and_oversized_frames():
  for header in (P.pack_header(8, 1, 0), P.pack_header(P.Msg.HUD, 1, MAX_MESSAGE + 1), b'JLNK' + bytes(28)):
    receiver = MemoryTransport(header)
    with pytest.raises(LinkError):
      receiver.recv(.1)
    with pytest.raises(LinkError, match='desynced'):
      receiver.recv(.1)
  with pytest.raises(LinkError):
    MemoryTransport().send(P.Msg.HUD, 0, [bytes(MAX_MESSAGE + 1)])


def test_superspeed_descriptors_have_counts_before_tables():
  descriptors = build_descriptors()
  assert struct.unpack_from('<IIIIII', descriptors) == (3, len(descriptors), 7, 3, 3, 5)
  assert descriptors[24:26] == bytes([9, 4])


def test_write_watchdog_expires_and_cannot_rearm():
  expired = threading.Event()
  guard = WriteWatchdog(expired.set)
  try:
    assert guard.arm(.01)
    assert expired.wait(.5)
    assert not guard.disarm()
    assert not guard.arm(1)
  finally:
    guard.close()
    guard.thread.join(.5)


def test_peer_stale_replay_and_reboot():
  peer = Peer('jetson')
  raw = b'{"role":"jetson","session":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","temperature_c":42}'
  assert not peer.alive(0)
  assert peer.accept(raw, 1, 10)
  assert not peer.accept(raw, 1, 11.9)
  assert not peer.alive(12)
  assert not display_status(peer, 12)['usb_connected']
  with pytest.raises(ValueError):
    peer.accept(raw.replace(b'aaa', b'bbb'), 2, 12)
  assert Peer('jetson').accept(raw, 0, 20)  # new connection after reboot


@pytest.mark.parametrize('raw', [b'[]', b'{"x":NaN}', b'{"x":Infinity}', b'x' * 4097])
def test_bad_heartbeat(raw):
  with pytest.raises(ValueError):
    Peer('jetson').accept(raw, 1, 1)


def test_disk_status_expires_and_video_resync(tmp_path):
  path = tmp_path / 'status.json'
  atomic_json(path, {'updated': 10})
  assert read_fresh(path, now=11)
  assert read_fresh(path, now=12) is None
  assert read_fresh(path, now=9) is None
  gate = VideoGate()
  assert not gate.accept(1, False, b'')
  assert gate.accept(2, True, b'header')
  assert gate.accept(3, False, b'')
  assert not gate.accept(5, False, b'')
  assert gate.accept(6, True, b'header')
  gate.reset()
  assert not gate.accept(7, False, b'')


def test_large_record_fragmentation_and_reconnect():
  raw = b'x' * MAX_RECORD
  packets = list(fragments(raw, 10))
  assert max(map(len, packets)) + P.HEADER_SIZE <= 128 * 1024
  assembler = Assembler()
  for packet in packets[:-1]:
    assert assembler.feed(P.Msg.ROAD_VIDEO, packet, 10) is None
  assert assembler.feed(P.Msg.ROAD_VIDEO, packets[-1], 10.2) == raw
  # A reconnect loses incomplete state; a trailing old fragment is discarded.
  assert Assembler().feed(P.Msg.ROAD_VIDEO, packets[1], 20) is None
  assert assembler.feed(P.Msg.HUD, packets[0], 20) is None
  assert assembler.feed(P.Msg.HUD, packets[1], 22) is None
  with pytest.raises(ValueError):
    list(fragments(bytes(MAX_RECORD + 1), 0))
  with pytest.raises(ValueError):
    assembler.feed(P.Msg.HUD, bytes(CHUNK + 100), 30)
