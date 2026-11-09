"""Frame-delimited Jetson libx264 output. Only Annex B pictures go to USB."""
from collections import deque
from io import BufferedReader
import struct
import threading
import time

from openpilot.tools.jetson.hud_pipeline_trace import HUD_TRACE


def avi_pictures(stream):
  """Read the single H264 stream from FFmpeg's non-seekable AVI muxer."""
  def exact(size):
    data = stream.read(size)
    if len(data) != size:
      raise EOFError('Incomplete FFmpeg AVI chunk')
    return data

  header = exact(12)
  if header[:4] != b'RIFF' or header[8:] != b'AVI ':
    raise ValueError('Expected FFmpeg AVI stream')
  in_movie = False
  while header := stream.read(8):
    if len(header) != 8:
      raise EOFError('Truncated AVI chunk header')
    kind, size = struct.unpack('<4sI', header)
    if not in_movie and kind == b'LIST':
      name = exact(4)
      if name == b'movi':
        in_movie = True
        continue
      if size < 4:
        raise ValueError('Invalid AVI list size')
      size -= 4
    if size > 2 * 1024 * 1024:
      raise ValueError('FFmpeg AVI chunk exceeds HUD buffer limit')
    payload = exact(size)
    if size & 1:
      exact(1)
    if in_movie:
      if kind == b'00dc' and not size:
        continue  # AVI timestamp padding, no corresponding encoded picture.
      if kind != b'00dc' or not payload.startswith((b'\x00\x00\x01', b'\x00\x00\x00\x01')):
        raise ValueError('Expected one Annex B video packet')
      yield payload
  if not in_movie:
    raise EOFError('Missing AVI movie list')


def pipeline_class(original, timing):
  class Pipeline(original):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      self._hud_frames = deque()
      self._hud_lock = threading.Lock()
      self.hud_expired_frames = 0
      self._layout = None
      self._safe_idr = None

    def _resolve_ffmpeg_output_muxer(self, ffmpeg):
      if 'avi' not in self._available_ffmpeg_muxers(ffmpeg):
        raise RuntimeError('Jetson HUD requires the FFmpeg AVI muxer')
      return 'avi'

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
      from openpilot.tools.jetson.hud import waiting_frame
      self.check_error()
      frame = {key: timing.get(key) for key in ('capture_mono', 'display_mode', 'frame_expires_mono')}
      frame['reason'] = (timing.get('input_gap') or {}).get('reason') if frame['display_mode'] == 'waiting_data' else None
      layout = (width, height, timing.get('portrait_upload', False), timing.get('flip_x', False))
      prime = layout != self._layout and frame.get('display_mode') == 'vehicle'
      with self._hud_lock:
        if len(self._hud_frames) + 1 + int(prime) > 4:
          # The output thread drains in order. Skip this new capture before
          # writing stdin, so a brief scheduling delay cannot reset the panel.
          timing['encoder_queue_skips'] = timing.get('encoder_queue_skips', 0) + 1
          HUD_TRACE.event('encoder_queue_full', queued_frames=len(self._hud_frames))
          return
        if prime:
          self._hud_frames.append({'prime': True})
        self._hud_frames.append(frame)
      if prime:
        super().submit_rgba(waiting_frame(*layout), width, height)
        self._layout = layout
      return super().submit_rgba(rgba, width, height)

    def _hud_access_unit(self, raw):
      with self._hud_lock:
        if not self._hud_frames:
          raise RuntimeError('HUD encoded frame has no input freshness record')
        frame = self._hud_frames.popleft()
      if frame.get('prime'):
        self._safe_idr = raw
        return
      mode = frame.get('display_mode')
      expires = frame.get('frame_expires_mono')
      now = time.monotonic()
      timing['encoder_wait_ms'] = (now - frame['capture_mono']) * 1000
      expired = expires is not None and now >= expires
      timing['output_reason'] = 'encoded_vehicle_expired' if expired else frame['reason']
      if expired:
        self.hud_expired_frames += 1
        timing['encoded_expired_frames'] = self.hud_expired_frames
        HUD_TRACE.encoded(expired=True, mode=frame.get('display_mode', 'unknown'),
                          lease_remaining_s=expires - now, wait_ms=timing['encoder_wait_ms'])
        # GOP=1 + repeat-headers: this independent neutral IDR has the same
        # dimensions/orientation and contains no vehicle or camera pixels.
        raw, mode = self._safe_idr, 'waiting_data'
        if raw is None:
          raise RuntimeError('Missing neutral HUD access unit')
      chunks = self._packetize_h264_for_usb(raw, max(1, self.chunk_size))
      self._write_dump(raw)
      self._record_h264_unit('ffmpeg', raw, chunks)
      timing['output_mode'] = mode
      for index, chunk in enumerate(chunks):
        timing['output_final_chunk'] = index == len(chunks) - 1
        # A slow physical USB write can cross the lease between chunks. Finish
        # the frame to preserve framing; the next frame is independently gated.
        self._send_h264_chunk(chunk, self.chunk_size, source='ffmpeg')
      timing['last_encoded_mode'] = mode
      HUD_TRACE.encoded(expired=False, mode=mode)

    def _read_stdout(self):
      try:
        with BufferedReader(self._proc.stdout) as stream:
          for picture in avi_pictures(stream):
            if self._closing:
              continue
            self._debug_stdout_reads += 1
            self._debug_stdout_bytes += len(picture)
            self._hud_access_unit(picture)
        if not self._closing:
          raise EOFError('FFmpeg video pipe closed during streaming')
      except BaseException as error:
        HUD_TRACE.event('encoder_read_failed', error_type=type(error).__name__)
        with self._condition:
          if not self._closing:
            self._error = error
          self._condition.notify_all()

  return Pipeline
