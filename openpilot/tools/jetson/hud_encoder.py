"""Jetson-only whole-frame expiry gate for the existing software H264 encoder.

The HUD already uses GOP=1. AUD boundaries let us discard an entire expired IDR
without sending a partial picture or breaking reference frames. One frame of
lookahead is intentional; no unbounded raw/encoded backlog is allowed.
"""
from collections import deque
import os
import re
import threading
import time

AUD = re.compile(b'\x00\x00\x00?\x01\x09')


def pipeline_class(original, timing):
  class Pipeline(original):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      self._hud_frames = deque()
      self._hud_lock = threading.Lock()
      self.hud_expired_frames = 0

    def _ffmpeg_command(self, ffmpeg, encoder, output_muxer):
      if encoder != 'libx264' or self.gop != 1:
        raise ValueError('Jetson HUD expiry gate requires libx264 GOP=1')
      command = super()._ffmpeg_command(ffmpeg, encoder, output_muxer)
      # Input nobuffer discards the probe frame and loses input/output pairing.
      index = command.index('-fflags')
      del command[index:index + 2]
      index = command.index('-x264-params') + 1
      command[index] += ':aud=1'
      return command

    def submit_rgba(self, rgba, width, height):
      from openpilot.tools.jetson.hud import EncoderStalled
      with self._hud_lock:
        if len(self._hud_frames) >= 4:
          raise EncoderStalled('HUD encoder backlog exceeded four frames')
        self._hud_frames.append(dict(timing))
      return super().submit_rgba(rgba, width, height)

    def _hud_access_unit(self, raw):
      from openpilot.tools.jetson.hud import EncoderStalled
      with self._hud_lock:
        if not self._hud_frames:
          raise RuntimeError('HUD encoded frame has no input freshness record')
        frame = self._hud_frames.popleft()
      expires = frame.get('frame_expires_mono')
      if expires is not None and time.monotonic() >= expires:
        self.hud_expired_frames += 1
        timing['encoded_expired_frames'] = self.hud_expired_frames
        return  # Complete independent IDR only; never drop an arbitrary chunk.
      if time.monotonic() - frame['capture_mono'] >= .5:
        raise EncoderStalled('HUD encoder output stalled beyond 500 ms')
      chunks = self._packetize_h264_for_usb(raw, max(1, self.chunk_size))
      self._write_dump(raw)
      self._record_h264_unit('ffmpeg', raw, chunks)
      timing['output_mode'] = frame.get('display_mode')
      for chunk in chunks:
        # A slow physical USB write can cross the lease between chunks. Finish
        # the frame to preserve framing; the next frame is independently gated.
        self._send_h264_chunk(chunk, self.chunk_size, source='ffmpeg')
      timing['last_encoded_mode'] = frame.get('display_mode')

    def _read_stdout(self):
      pending = bytearray()
      try:
        while True:
          raw = os.read(self._proc.stdout.fileno(), 65536)
          if not raw:
            return
          pending.extend(raw)
          self._debug_stdout_reads += 1
          self._debug_stdout_bytes += len(raw)
          if len(pending) > 2 * 1024 * 1024:
            raise RuntimeError('HUD encoder access unit exceeds bounded buffer')
          while True:
            boundaries = list(AUD.finditer(pending))
            if len(boundaries) < 2:
              break
            end = boundaries[1].start()
            self._hud_access_unit(bytes(pending[:end]))
            del pending[:end]
      except BaseException as error:
        with self._condition:
          if not self._closing:
            self._error = error
          self._condition.notify_all()

  return Pipeline
