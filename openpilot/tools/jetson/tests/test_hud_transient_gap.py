"""Delayed real TCP and real software codec tests, without claiming USB pixels."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport
from openpilot.selfdrive.modeld.jetlink.protocol import Msg
from openpilot.tools.jetson import hud, hud_tx, ncm_display
from openpilot.tools.jetson.transport.base import LinkError


@contextmanager
def tcp_pair():
  with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    with socket.create_connection(listener.getsockname()) as sock:
      peer, _ = listener.accept()
      with peer:
        yield JetlinkNcmTcpTransport(sock), JetlinkNcmTcpTransport(peer)


class DelayedSocket:
  def __init__(self, sock, delay, partial=0):
    self.sock, self.until, self.partial = sock, time.monotonic() + delay, partial
  def __getattr__(self, key):
    return getattr(self.sock, key)
  def send(self, raw):
    if self.partial:
      count, self.partial = self.partial, 0
      return self.sock.send(raw[:count])
    if time.monotonic() < self.until:
      raise BlockingIOError()
    return self.sock.send(raw)
  def sendmsg(self, parts):
    return self.send(b''.join(parts))


def packet():
  return json.dumps({'version': 1, 'sent': time.monotonic(), 'session': 'a' * 32,
    'events': {}, 'params': {}, 'cameras': {}, 'mono': {}, 'received': {}, 'valid': {}, 'alive': {}}).encode()


def test_12ms_deadline_abandons_partial_stream_but_hud_writer_resumes():
  with tcp_pair() as (original, peer):
    original.sock = DelayedSocket(original.sock, .06, partial=8)
    with pytest.raises(LinkError):
      original.send(Msg.HUD, 1, [packet()], timeout=.012)
    assert original.last_send['failed'] and original.last_send['bytes_written'] == 8
  with tcp_pair() as (original, peer):
    original.sock = DelayedSocket(original.sock, .06, partial=8)
    tx = hud_tx.HudTx(original)
    tx.send(Msg.HUD, 1, [packet()], timeout=.012)
    deadline = time.monotonic() + 1
    ticks = 0
    while not tx.idle and time.monotonic() < deadline:
      tx.pump()
      ticks += 1
      time.sleep(.005)
    msg = peer.recv(.2)
    assert msg.seq == 1 and json.loads(msg.payload.tobytes())['hud_link']['seq'] == 1
    assert ticks > 2 and not tx.last_send['failed']
    assert tx.status['hud_send_ms'] >= 40


def test_latest_unsent_snapshot_replaces_old_without_reordering_or_unbounded_queue():
  with tcp_pair() as (original, peer):
    tx = hud_tx.HudTx(original)
    for seq in range(1, 101):
      tx.send(Msg.HUD, seq, [packet()])
    assert len(tx.queue) == 1 and tx.status['tx_coalesced'] == 99
    tx.pump()
    assert peer.recv(.1).seq == 100
    assert tx.last_hud_seq == 100


def test_partial_expiry_closes_but_unsent_expiry_drops_and_retains_connection():
  for partial in (0, 8):
    with tcp_pair() as (original, peer):
      original.sock = DelayedSocket(original.sock, 1, partial=partial)
      now = [time.monotonic()]
      tx = hud_tx.HudTx(original, lambda: now[0])
      tx.send(Msg.HUD, 1, [packet()])
      tx.pump()
      now[0] += .31
      if partial:
        with pytest.raises(LinkError, match='partial message'):
          tx.pump()
        assert tx.last_send['error_reason'] == 'partial_send_expired'
      else:
        tx.pump()
        assert tx.idle and tx.status['tx_dropped'] == 1 and original.sock.fileno() >= 0


def test_broken_pipe_is_recorded_and_stream_closed():
  with tcp_pair() as (original, peer):
    class Broken(DelayedSocket):
      def send(self, raw):
        raise BrokenPipeError(32, 'injected broken pipe')
    original.sock = Broken(original.sock, 0)
    tx = hud_tx.HudTx(original)
    tx.send(Msg.HUD, 1, [packet()])
    with pytest.raises(BrokenPipeError):
      tx.pump()
    assert tx.last_send['errno'] == 32 and tx.last_send['failed']
    assert original.sock.fileno() == -1


def test_clock_bound_prevents_delayed_packet_becoming_fresh_again(tmp_path, monkeypatch):
  monkeypatch.setattr(ncm_display, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(ncm_display, 'RUNTIME', tmp_path)
  now = [9000.]
  transport = SimpleNamespace(send_json=lambda *a: None, close=lambda: None)
  session = ncm_display.DisplaySession(transport, SimpleNamespace(close=lambda: None), lambda: now[0])
  def message(kind, seq, value):
    return SimpleNamespace(msg_type=kind, seq=seq, payload=json.dumps(value).encode())
  session.handle(message(Msg.HELLO_REQ, 1, {'client': {'name': 'nexo-ncm-display'}}))
  value = json.loads(packet())
  value['sent'] = 100.
  value['hud_link'] = {'connection_id': 'b' * 32, 'seq': 2, 'comma_produced_mono': 100.,
                       'comma_tx_mono': 100.01, 'jetson_clock_floor': 8900.}
  now[0] += .7  # TCP-delayed data, not a new generation.
  session.handle(message(Msg.HUD, 2, value))
  saved = json.loads(ncm_display.SNAPSHOT.read_text())
  assert saved['updated'] == 9000. and now[0] - saved['updated'] > .5
  assert session.link['source_age_upper_s'] == pytest.approx(.7)


def test_waiting_screen_preserves_both_mirror_modes():
  from PIL import Image
  normal = Image.frombytes('RGBA', (80, 320), hud.waiting_frame(80, 320, True, False))
  mirrored = Image.frombytes('RGBA', (80, 320), hud.waiting_frame(80, 320, True, True))
  assert normal.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes() == mirrored.tobytes()


def test_encoder_backlog_skips_unsubmitted_input_without_shifting_metadata():
  from openpilot.tools.jetson.hud_encoder import pipeline_class
  class Encoder:
    def check_error(self):
      pass
    def submit_rgba(self, *args):
      pass
  timing = {'capture_mono': 0}
  pipeline = pipeline_class(Encoder, timing)()
  for i in range(5):
    timing['capture_mono'] = i
    pipeline.submit_rgba(b'', 1, 1)
  assert timing['encoder_queue_skips'] == 1
  assert [frame['capture_mono'] for frame in pipeline._hud_frames] == [0, 1, 2, 3]


def test_state_observation_distinguishes_source_stall_from_tcp_delay(tmp_path, monkeypatch):
  monkeypatch.setattr(ncm_display, 'SNAPSHOT', tmp_path / 'hud.json')
  monkeypatch.setattr(ncm_display, 'RUNTIME', tmp_path)
  session = ncm_display.DisplaySession(SimpleNamespace(send_json=lambda *a: None), None, lambda: 9000.)
  session.hello = True
  def state(seq, produced):
    return SimpleNamespace(msg_type=Msg.STATE_REQ, seq=seq,
      payload=json.dumps({'comma_state_mono': 100., 'comma_produced_mono': produced}).encode())
  session.handle(state(1, 99.))
  assert session.source_state['reason'] == 'snapshot_producer_stalled'
  session.handle(state(2, 99.95))
  assert session.source_state['reason'] == 'source_running'
  assert session.last_hud is None  # TCP is alive, but HUD has not arrived.


def test_actual_encoder_survives_gap_and_decodes_waiting_then_fresh_frames(tmp_path, monkeypatch):
  ffmpeg = os.environ.get('NEXO_TEST_FFMPEG')
  if not ffmpeg:
    pytest.skip('Set NEXO_TEST_FFMPEG to exercise actual libx264')
  import numpy as np
  sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'selfdrive/carrot/cluster'))
  from cluster_h264_pipeline import H264UsbPipeline
  from openpilot.tools.jetson.hud_encoder import pipeline_class
  sent, starts, stops = [], [], []
  panel = SimpleNamespace(start_h264_stream=lambda _: starts.append(True) or 65536,
    stop_h264_stream=lambda: stops.append(True), send_h264_chunk=lambda raw, **kw: sent.append(raw))
  timing = {}
  pipeline = pipeline_class(H264UsbPipeline, timing)(panel, 64, 64, 1, 10, '200k', 1,
    'ffmpeg', '', ffmpeg, 'libx264', '', 'nv12', 0, 'vbr-cfr', False, 65536, False, False, '', False,
    ffmpeg_threads=1)
  pipeline.start()
  pid = pipeline._proc.pid
  try:
    for index in range(16):
      now = time.monotonic()
      mode = 'vehicle' if index < 4 or index >= 10 else 'waiting_data'
      color = (240, 0, 0, 255) if index < 4 else (0, 240, 0, 255) if index >= 10 else (8, 12, 16, 255)
      # One deliberately expired red frame must never reach the decoder.
      expires = now - 1 if index == 0 else now + .5 if mode == 'vehicle' else None
      timing.update(capture_mono=now, frame_expires_mono=expires, display_mode=mode)
      pixels = hud.waiting_frame(64, 64, False) if mode == 'waiting_data' else bytes(color) * (64 * 64)
      pipeline.submit_rgba(pixels, 64, 64)
      deadline = time.monotonic() + 1
      while len(sent) <= index and time.monotonic() < deadline:
        pipeline.check_error()
        time.sleep(.005)
      assert len(sent) == index + 1  # no next RGBA submission or EOF required
      pipeline.check_error()
      assert pipeline._proc.pid == pid and pipeline._proc.poll() is None
    assert starts == [True] and not stops and pipeline.hud_expired_frames == 1
  finally:
    pipeline.close()
  assert stops == [True]
  decoded = subprocess.run([ffmpeg, '-v', 'error', '-f', 'h264', '-i', 'pipe:0',
                            '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                           input=b''.join(sent), capture_output=True, check=True, timeout=10).stdout
  frames = np.frombuffer(decoded, np.uint8).reshape(-1, 64, 64, 3).mean(axis=(1, 2))
  assert len(frames) == 16
  assert frames[0][0] < 100  # neutral IDR replaces the expired first red frame
  assert all(f[0] > 200 and f[1] < 30 for f in frames[1:4])
  assert all(35 < min(f) and max(f) < 120 for f in frames[4:10])
  assert np.allclose(frames[0], frames[4], atol=4)  # neutral IDR matches the actual waiting scene
  assert all(f[1] > 200 and f[0] < 30 for f in frames[10:])

def test_avi_reader_handles_large_split_packet_and_rejects_truncation():
  from io import BufferedReader, BytesIO
  import struct
  from openpilot.tools.jetson.hud_encoder import avi_pictures
  class Fragmented(BytesIO):
    def readinto(self, buffer):
      return super().readinto(memoryview(buffer)[:17])
  payload = b'\x00\x00\x00\x01\x65' + bytes(70000)
  movie = b'RIFF' + bytes(4) + b'AVI LIST' + bytes.fromhex('ffffffff') + b'movi00dc'
  stream = movie + struct.pack('<I', len(payload)) + payload + b'\x00'
  assert list(avi_pictures(BufferedReader(Fragmented(stream)))) == [payload]
  with pytest.raises(EOFError):
    list(avi_pictures(BytesIO(stream[:-20])))
  with pytest.raises(ValueError):
    list(avi_pictures(BytesIO(movie + struct.pack('<I', 0xffffffff))))

@pytest.mark.parametrize('wait,strict', [(False, False), (True, False), (True, True)])
def test_short_usb_out_never_counts_as_panel_write(wait, strict, monkeypatch):
  from threading import RLock
  monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'selfdrive/carrot/cluster'))
  from cluster_usb_display import TuringUsbDisplay
  panel = TuringUsbDisplay()
  panel.dev = object()
  panel._usb_lock = RLock()
  panel._ep_out = SimpleNamespace(write=lambda payload, timeout: len(payload) - 1)
  panel._ep_in = SimpleNamespace(read=lambda *a: b'')
  monkeypatch.setattr(panel, '_build_h264_chunk_payload', lambda chunk, **kw: chunk)
  monkeypatch.setattr(panel, '_drain_input', lambda **kw: None)
  with pytest.raises(RuntimeError, match='write failed'):
    panel.send_h264_chunk(b'chunk', wait_for_ack=wait, require_ack_response=strict)
  panel._ep_out.write = lambda payload, timeout: len(payload)
  def timeout(*a):
    raise TimeoutError('optional reply')
  panel._ep_in.read = timeout
  monkeypatch.setattr(panel, '_h264_flow_control', lambda **kw: None)
  if wait and strict:
    with pytest.raises(RuntimeError, match='reply unavailable'):
      panel.send_h264_chunk(b'chunk', wait_for_ack=wait, require_ack_response=strict)
  else:
    panel.send_h264_chunk(b'chunk', wait_for_ack=wait, require_ack_response=strict)


@pytest.mark.parametrize('lag', [.1, .2, .5])
def test_one_late_state_reply_cannot_consume_the_vehicle_lease(lag):
  tx = hud_tx.HudTx(None, clock=lambda: 100., host_clock_floor=9000.)
  received = 100.25 + lag
  tx.observe_reply_clock(9100.25, received)
  floor = tx.clock_floor(received, received)
  assert 8999.998 <= floor <= 9000.
  # A real wire delay still consumes the original 500 ms source lease.
  assert received + 9000. + .5 - (received + floor) >= .5
  assert hud_tx.HudTx(None).clock_floor(received, received) is None


def test_camera_tx_and_host_receipt_only_after_completed_hud_packet(tmp_path, monkeypatch):
  from base64 import b64encode
  from io import BytesIO
  from PIL import Image
  from openpilot.tools.jetson.camera import preview_stamp
  jpg = BytesIO()
  Image.new('RGB', (384, 240)).save(jpg, 'JPEG')
  sample = json.loads(packet())
  sample['cameras']['road'] = {'width': 384, 'height': 240, 'time': sample['sent'] - .02,
                                'jpeg': b64encode(jpg.getvalue()).decode()}
  with tcp_pair() as (transport, peer):
    writer = hud_tx.HudTx(transport)
    writer.send(Msg.HUD, 1, [json.dumps(sample).encode()])
    assert 'last_camera_tx_mono' not in writer.status
    writer.pump()
    assert writer.status['last_camera_tx_mono'] >= sample['sent']
    monkeypatch.setattr(ncm_display, 'SNAPSHOT', tmp_path / 'hud.json')
    session = ncm_display.DisplaySession(SimpleNamespace(), None, lambda: 10000.)
    session.hello = True
    session.handle(peer.recv(.2))
    assert session.camera_received == 10000.
    assert .019 < 10000. - session.camera_capture < .3
  sample['cameras']['road']['time'] -= .5
  assert preview_stamp(sample) is None


def test_waiting_font_failure_uses_ascii_safe_fallback(monkeypatch):
  from PIL import Image, ImageFont
  from openpilot.tools.jetson import hud
  hud.waiting_frame.cache_clear()
  def missing_font(*args, **kwargs):
    raise OSError('font unavailable')
  monkeypatch.setattr(ImageFont, 'truetype', missing_font)
  try:
    pixels = hud.waiting_frame(80, 320, True, False)
    assert len(pixels) == 80 * 320 * 4
  finally:
    hud.waiting_frame.cache_clear()
