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


def test_encoder_backlog_is_output_failure_not_snapshot_wait():
  from openpilot.tools.jetson.hud_encoder import pipeline_class
  class Encoder:
    def __init__(self):
      pass
    def submit_rgba(self, *args):
      pass
  pipeline = pipeline_class(Encoder, {'capture_mono': time.monotonic()})()
  for _ in range(4):
    pipeline.submit_rgba(b'', 1, 1)
  with pytest.raises(hud.EncoderStalled) as failure:
    pipeline.submit_rgba(b'', 1, 1)
  assert hud.failure_reason(failure.value) == 'encoder_output_stalled'


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
      pipeline.submit_rgba(bytes(color) * (64 * 64), 64, 64)
      time.sleep(.1)
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
  assert len(frames) == 14  # 16 inputs minus expired frame minus one lookahead.
  assert all(f[0] > 200 and f[1] < 30 for f in frames[:3])
  assert all(max(f) < 30 for f in frames[3:9])
  assert all(f[1] > 200 and f[0] < 30 for f in frames[9:])
