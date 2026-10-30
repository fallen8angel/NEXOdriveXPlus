"""Cooperative TCP writer used ONLY by the separate Display Only process.

12 ms is a scheduling slice, not a TCP message lifetime. Keep a partial frame
until its freshness deadline, poll RX between writes, and coalesce only frames
whose header has not reached the wire. Never reuse a partially expired stream.
"""
import json
import select
import time
import uuid

from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.tools.jetson.transport.base import LinkError


class HudTx:
  def __init__(self, transport, clock=time.monotonic, host_clock_floor=None):
    self.transport, self.clock = transport, clock
    self.host_clock_floor = host_clock_floor
    self.queue, self.active = [], None
    self.connection_id = uuid.uuid4().hex
    self.last_hud_seq = None
    self.status = {'tx_coalesced': 0, 'tx_dropped': 0}
    self.last_send = {}

  def __getattr__(self, key):
    return getattr(self.transport, key)

  @property
  def idle(self):
    return self.active is None and not self.queue

  def send(self, kind, seq, parts=(), flags=0, timeout=None):
    if kind not in (P.Msg.HUD, P.Msg.NAVI_MEDIA, P.Msg.STATE_REQ):
      raise ValueError('HUD writer cannot carry inference traffic')
    now = self.clock()
    raw = b''.join(parts)
    produced = now
    if kind == P.Msg.HUD:
      produced = json.loads(raw)['sent']
      old = len(self.queue)
      self.queue = [item for item in self.queue if item['kind'] != kind]
      self.status['tx_coalesced'] += old - len(self.queue)
    if len(self.queue) >= 3:
      # Navigation fragments are optional; its reassembler detects missing parts.
      self.queue = [item for item in self.queue if item['kind'] != P.Msg.NAVI_MEDIA]
      if len(self.queue) >= 3:
        raise LinkError('HUD transmit queue overflow')
    self.queue.append(dict(kind=kind, seq=seq, raw=raw, flags=flags, produced=produced,
                           queued=now, deadline=min(now + .3, produced + .3)))

  def pump(self, budget=.012):
    end = self.clock() + budget
    # No select wait: RX/STATE and production keep running under backpressure.
    for _ in range(16):
      now = self.clock()
      if now >= end:
        break
      if self.active is None:
        if not self.queue:
          break
        item = self.queue.pop(0)
        if now >= item['deadline']:
          self.status['tx_dropped'] += 1
          continue
        raw = item['raw']
        if item['kind'] == P.Msg.HUD:
          value = json.loads(raw)
          value['hud_link'] = {'connection_id': self.connection_id, 'seq': item['seq'],
                               'comma_produced_mono': item['produced'], 'comma_tx_mono': now}
          if self.host_clock_floor is not None:
            value['hud_link']['jetson_clock_floor'] = self.host_clock_floor
          raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
          # Preserve the established HUD size limit, including optional evidence.
          if len(raw) > 96 * 1024:
            from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet
            raw = snapshot_packet(raw, value['session'])
        flags = item['flags']
        pad = (P.HEADER_SIZE + len(raw)) % P.PACKET_MULTIPLE == 0
        if pad:
          flags |= P.Flag.PADDED
        item.update(wire=P.pack_header(item['kind'], item['seq'], len(raw), flags) + raw + (b'\0' if pad else b''),
                    offset=0, started=now)
        self.active = item
        self.last_send = dict(kind=int(item['kind']), seq=item['seq'], bytes_written=0, failed=False,
                              comma_tx_start_mono=now, queue_ms=(now - item['queued']) * 1000)
      item = self.active
      self.last_send.update(elapsed_s=now - item['started'], pending_bytes=len(item['wire']) - item['offset'],
                            tx_waiting=True, rx_buffered_bytes=self.transport.rx.available)
      if now >= item['deadline']:
        if item['offset'] == 0:
          self.active = None
          self.status['tx_dropped'] += 1
          continue
        self._fail(LinkError('HUD partial message freshness deadline expired'), 'partial_send_expired')
      try:
        readable, writable, _ = select.select([self.sock], [self.sock], [], 0)
        self.last_send['rx_readable'] = bool(readable)
        if not writable:
          break
        count = self.sock.send(memoryview(item['wire'])[item['offset']:])
        if count <= 0:
          raise BrokenPipeError('peer closed during HUD send')
      except (BlockingIOError, InterruptedError):
        break
      except OSError as error:
        self._fail(error, type(error).__name__)
      item['offset'] += count
      self.last_send['bytes_written'] = item['offset']
      if item['offset'] == len(item['wire']):
        finished = self.clock()
        self.last_send.update(tx_waiting=False, pending_bytes=0, elapsed_s=finished - item['started'])
        if item['kind'] == P.Msg.HUD:
          self.last_hud_seq = item['seq']
          self.status.update(last_hud_tx_mono=finished, hud_send_ms=(finished - item['started']) * 1000,
                             hud_source_age_s=finished - item['produced'], hud_wire_seq=item['seq'])
        self.active = None
    self.status.update(wire_send=dict(self.last_send), tx_queue_depth=len(self.queue) + int(self.active is not None),
                       connection_id=self.connection_id)

  def _fail(self, error, reason):
    self.last_send.update(failed=True, error_type=type(error).__name__, error_reason=reason,
                          errno=getattr(error, 'errno', None))
    self.close()
    raise error

  def close(self):
    self.queue.clear()
    self.active = None
    self.transport.close()
