"""Deterministic producer/publisher timing; no vehicle or panel hardware."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpilot.selfdrive.modeld.jetlink import display


class DisplayPollTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.publisher = display.DisplayPublisher.__new__(display.DisplayPublisher)
    p = self.publisher
    p.path = Path(self.temp.name) / 'display.packet'
    p.process = SimpleNamespace(poll=lambda: None)
    p.standalone = True
    p.last_sent, p.next_hud, p.hud_connected = 0., 0., False
    p.status = {}
    p.media_socket = None
    self.now = 100.
    self.sent = []
    self.client = SimpleNamespace(last_state={}, _next_seq=lambda: len(self.sent) + 1,
                                  t=SimpleNamespace(send=lambda *a, **kw: self.sent.append((self.now, a[2][0]))))

  def write_packet(self, stamp, payload):
    self.publisher.path.write_bytes(display.HEADER.pack(stamp) + payload)

  def poll(self, now):
    self.now = now
    self.publisher.send(self.client, clock=lambda: self.now)

  def test_standalone_polls_new_worker_frame_without_waiting_a_second_100ms_cycle(self):
    self.write_packet(100., b'first')
    self.poll(100.)
    self.write_packet(100.031, b'second')
    self.poll(100.04)
    self.assertEqual(self.sent, [(100., b'first'), (100.04, b'second')])
    self.poll(100.08)
    self.assertEqual(len(self.sent), 2)  # No repeat for the same worker frame.

  def test_out_of_order_expired_and_future_packets_are_not_sent(self):
    self.write_packet(100., b'first')
    self.poll(100.)
    for now, stamp in ((100.04, 99.99), (100.08, 99.7), (100.12, 101.)):
      self.write_packet(stamp, b'bad')
      self.poll(now)
    self.assertEqual(self.sent, [(100., b'first')])
    self.write_packet(100.13, b'reconnected')
    self.poll(100.16)
    self.assertEqual(self.sent[-1][1], b'reconnected')

  def test_inference_owner_retains_original_100ms_cadence(self):
    self.publisher.standalone = False
    self.write_packet(100., b'first')
    self.poll(100.)
    self.write_packet(100.031, b'second')
    self.poll(100.04)
    self.assertEqual(len(self.sent), 1)
    self.poll(100.11)
    self.assertEqual(self.sent[-1][1], b'second')

  def test_new_but_expired_packet_is_not_sent(self):
    self.write_packet(99.6, b'expired')
    self.poll(100.)
    self.assertFalse(self.sent)
    self.write_packet(100.03, b'fresh')
    self.poll(100.04)
    self.assertEqual(self.sent[-1][1], b'fresh')


class HudSafetyTest(unittest.TestCase):
  def test_guard_retains_reserve_expiry_and_recovers_after_disconnect(self):
    from openpilot.tools.jetson import hud
    now = [100.]
    current = [{'updated': 99.75}]
    vehicle = bytes((245, 15, 0, 255)) * (320 * 80)

    @contextmanager
    def capture():
      yield vehicle, 320, 80

    with tempfile.TemporaryDirectory() as directory, \
         patch.object(hud, 'RUNTIME', Path(directory)), \
         patch.object(hud, 'read_snapshot', side_effect=lambda: current[0]), \
         patch.object(hud.time, 'monotonic', side_effect=lambda: now[0]), \
         patch.object(hud, 'read_fresh', return_value={'state': 'connected', 'updated': 100.}):
      guarded = hud.guarded_capture(capture)
      for age, expected_vehicle in ((.25, True), (.31, False), (.49, False), (.5, False)):
        current[0] = {'updated': now[0] - age}
        with guarded() as (raw, width, height):
          self.assertEqual(raw == vehicle, expected_vehicle)
          if not expected_vehicle:
            self.assertEqual(raw, hud.waiting_frame(width, height, False))
      current[0] = None
      with guarded() as (raw, width, height):
        self.assertEqual(raw, hud.waiting_frame(width, height, False))
      current[0] = {'updated': now[0]}
      with guarded() as (raw, _, _):
        self.assertEqual(raw, vehicle)


if __name__ == '__main__':
  unittest.main()
