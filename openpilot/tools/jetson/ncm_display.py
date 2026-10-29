"""Explicit NCM HUD-only sessions. No engine, model upload, or inference requests."""
import logging
from collections import deque
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from openpilot.selfdrive.modeld.jetlink import enabled
from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
from openpilot.selfdrive.modeld.jetlink.display import CAPABILITY, HUD_LIMIT, DisplayPublisher
from openpilot.selfdrive.modeld.jetlink.protocol import Msg, VERSION
from openpilot.tools.jetson.snapshot import validate_snapshot
from openpilot.tools.jetson.state import RUNTIME, SNAPSHOT, atomic_json, decode_json, public_telemetry, read_fresh
from openpilot.tools.jetson.transport.base import LinkTimeout

OPT_IN = Path('/data/nexo_jetson_hud_ncm')
STATUS = RUNTIME / 'ncm-display.json'
DISPLAY_CAPABILITY = 'nexo_display_only_v1'
ACK_CAPABILITY = 'nexo_display_ack_v1'
HELLO_TIMEOUT = 3.
IDLE_TIMEOUT = 5.
# Display-only transport must reconnect promptly after a transient USB-NCM
# dropout. Never shorten the separate model/inference owner's retry policy.
MAX_HUD_RECONNECT_DELAY = 8.


def hud_reconnect_delay(failures):
  return min(MAX_HUD_RECONNECT_DELAY, 2 ** min(max(0, failures), 3))

log = logging.getLogger(__name__)
_diagnostic_events = deque(maxlen=32)
_diagnostic_state = None
_last_observations = {}
_connection_anomalies = _transport_recoveries = 0
_last_transport_error = None


def requested(params):
  # Separate explicit display opt-in. Never touch the inference enable file or
  # compete with the old display-only USB gadget owner.
  return OPT_IN.is_file() and not enabled() and not params.get_bool('NexoJetsonUsb')


def publish(state, **values):
  global _diagnostic_state, _connection_anomalies, _transport_recoveries, _last_transport_error
  now = time.monotonic()
  if values.get('error'):
    from openpilot.tools.jetson.state import public_text
    _last_transport_error = {'time_unix': time.time(), 'detail': public_text(values['error']),
                             'domain': values.get('error_domain', 'tcp')}
  if state != _diagnostic_state:
    if _diagnostic_state == 'connected' and state == 'retrying':
      _connection_anomalies += 1
    if state == 'connected' and _diagnostic_state == 'retrying':
      _transport_recoveries += 1
    _diagnostic_events.append({'at': now, 'pid': os.getpid(), 'state': state,
                               'error': str(values.get('error', ''))[:400]})
    _diagnostic_state = state
  for key in ('last_hud_tx_mono', 'last_peer_rx_mono', 'peer'):
    if key in values:
      _last_observations[key] = values[key]
  try:
    atomic_json(STATUS, {'updated': now, 'state': state, 'transport': 'ncm-tcp',
                        'pid': os.getpid(), 'diagnostic_events': list(_diagnostic_events),
                        'last_transport_error': _last_transport_error,
                        'inference_enabled': False, **_last_observations, **values,
                        'hello_complete': state == 'connected' and values.get('hello_complete') is True,
                        'connection_anomalies': _connection_anomalies, 'transport_recoveries': _transport_recoveries})
  except OSError:
    pass  # ENOSPC/read-only diagnostics cannot terminate a healthy transport.


class DisplaySession:
  def __init__(self, transport, navi, clock=time.monotonic):
    from openpilot.tools.jetson.health import source_revision
    self.source_revision = source_revision()
    self.t, self.navi = transport, navi
    self.clock = clock
    self.last_activity = clock()
    self.last_seq, self.hello = -1, False
    self.last_hud = self.last_hud_seq = None
    self.rx_gap = self.store_ms = None
    SNAPSHOT.unlink(missing_ok=True)

  def check_alive(self):
    limit = IDLE_TIMEOUT if self.hello else HELLO_TIMEOUT
    if self.clock() - self.last_activity >= limit:
      raise TimeoutError('NCM display idle timeout' if self.hello else 'NCM display HELLO timeout')

  def handle(self, msg):
    self.check_alive()
    if msg.seq <= self.last_seq:
      raise ValueError('Non-increasing display sequence')
    self.last_seq = msg.seq
    if msg.msg_type == Msg.HELLO_REQ:
      if self.hello:
        raise ValueError('Display HELLO already completed')
      request = decode_json(bytes(msg.payload), 1024)
      if request.get('client', {}).get('name') != 'nexo-ncm-display':
        raise ValueError('This host is display-only; inference clients are refused')
      self.hello = True
      self.t.send_json(Msg.HELLO_RESP, msg.seq, {'protocol': VERSION, CAPABILITY: True,
                       DISPLAY_CAPABILITY: True, ACK_CAPABILITY: True,
                       'carrot_hud_v1': True, 'carrot_navi_v1': True})
      self.last_activity = self.clock()
      return
    if not self.hello:
      raise ValueError('Display HELLO required')
    if msg.msg_type == Msg.HUD:
      value = validate_snapshot(decode_json(bytes(msg.payload), HUD_LIMIT))
      if not isinstance(value.get('session'), str) or len(value['session']) != 32:
        raise ValueError('Invalid HUD generation')
      value['updated'] = self.clock()
      self.rx_gap = None if self.last_hud is None else value['updated'] - self.last_hud
      atomic_json(SNAPSHOT, value)
      self.store_ms = (self.clock() - value['updated']) * 1000
      self.last_hud, self.last_hud_seq = value['updated'], msg.seq
    elif msg.msg_type == Msg.NAVI_MEDIA:
      self.navi.send(msg.payload)
    elif msg.msg_type == Msg.STATE_REQ:
      # The NCM receipt confirms file storage only. A *separate* fresh renderer
      # report confirms the Jetson software finished a panel write, not pixels.
      # Read the panel once so its status and telemetry cannot disagree.
      panel = read_fresh(RUNTIME / 'hud-status.json', float('inf')) or {}
      from openpilot.tools.jetson.hud_diagnostics import renderer_evidence
      evidence = renderer_evidence(self.clock(), RUNTIME)
      recovery = read_fresh(RUNTIME / 'hud-recovery.json', 3600) or {}
      sent = panel.get('last_panel_send_mono')
      panel_age = self.clock() - sent if type(sent) in (int, float) else None
      if panel_age is not None and (not math.isfinite(panel_age) or panel_age < 0):
        panel_age = None
      panel_recent = panel_age is not None and panel_age < 2
      if evidence['recovery']['available']:
        panel_recent = panel_recent and evidence['renderer_running'] and panel.get('pid') == evidence['renderer_pid']
      telemetry = {'carrot_hud_connected': panel_recent,
                   'hud_data_received': self.last_hud is not None
                   and self.clock() - self.last_hud < .5}
      if recovery:
        # Never relay credentials, arbitrary stack traces or host file paths.
        telemetry.update(public_telemetry({'nexo_hud_recovery': recovery}))
      self.t.send_json(Msg.STATE_RESP, msg.seq, {'display_only': True, 'engine': None,
                       'source_revision': self.source_revision, 'host_pid': os.getpid(),
                       'hud_rx_seq': self.last_hud_seq,
                       'hud_rx_age': None if self.last_hud is None else self.clock() - self.last_hud,
                       'hud_rx_gap_s': self.rx_gap, 'hud_store_ms': self.store_ms,
                       'hud_diagnostics': evidence,
                       'hud_pipeline': {'renderer_pid': evidence['renderer_pid'] or (panel.get('pid') if panel_recent and type(panel.get('pid')) is int else None),
                                        'renderer_running': evidence['renderer_running'],
                                        'renderer_stage': evidence['renderer_stage'],
                                        'snapshot_age_s': panel.get('snapshot_age_s'),
                                        'panel_send_ms': panel.get('panel_send_ms'),
                                        'render_ms': panel.get('render_ms'),
                                        'render_expired': panel.get('render_expired'),
                                        'last_panel_send_age_s': panel_age if panel_recent else None,
                                        'last_successful_panel_age_s': panel_age if panel_age is not None else evidence['last_successful_panel_age_s'],
                                        'panel_write_recent': panel_recent,
                                        'physical_hud_verified': False},
                       'telemetry': telemetry})
    else:
      # Includes ENGINE, INFER, upload, Wi-Fi configuration and shutdown.
      raise ValueError(f'Operation forbidden on display-only host: {msg.msg_type}')
    self.last_activity = self.clock()

  def close(self):
    try:
      try:
        self.t.close()
      finally:
        self.navi.close()
    finally:
      SNAPSHOT.unlink(missing_ok=True)


def receive_session(session, timeout_error):
  while True:
    session.check_alive()
    try:
      session.handle(session.t.recv(.1))
    except timeout_error:
      continue
    publish('connected', last_hud_rx=session.last_hud)


def host_main():
  from openpilot.tools.jetson.hud_diagnostics import ServiceMonitor
  with ServiceMonitor():
    _host_main()


def _host_main():
  vendor = Path(os.environ['CARROT_JETSON'])
  sys.path[:0] = [str(vendor / 'tools/jetlink'), str(vendor / 'third_party/jetlink')]
  from hud_navi import HostForwarder
  from jetlink.transport.base import LinkTimeout
  from openpilot.tools.jetson.ncm_server import tcp_opener
  accept = tcp_opener(os.environ['NEXO_JETLINK_USB_IFACE'])(SimpleNamespace(host=ncm.JETSON_ADDR, port=ncm.PORT))
  try:
    while True:
      publish('waiting_comma_ncm')
      transport = accept()
      if transport is None:
        time.sleep(1)
        continue
      session = None
      try:
        session = DisplaySession(transport, HostForwarder())
        receive_session(session, LinkTimeout)
      except Exception as error:
        log.warning('NCM display session: %s', error)
        publish('retrying', error=str(error)[:400])
      finally:
        if session is not None:
          session.close()
        else:
          transport.close()
  finally:
    accept.close()


class HudReceipt:
  """Observe only this display client's writes; never change inference traffic.

  STATE follows completed HUD writes on the same ordered TCP stream. A capable
  peer must acknowledge exactly the last HUD sequence, after snapshot storage.
  Receipt confirms the host file, not physical TURZX output. Legacy peers remain
  usable but cannot claim this acknowledgement.
  """
  def __init__(self, transport, supported):
    self.transport, self.supported = transport, supported
    self.last_seq = None
    self.fresh_for = 0.

  def send(self, kind, seq, payload, **kwargs):
    self.transport.send(kind, seq, payload, **kwargs)
    if kind == Msg.HUD:
      self.last_seq = seq

  def check(self, state, expected_seq=-1):
    self.fresh_for = 0.
    if not self.supported:
      return False
    if not isinstance(state, dict):
      raise ValueError('Invalid NCM display state')
    seq, age = state.get('hud_rx_seq'), state.get('hud_rx_age')
    expected = self.last_seq if expected_seq == -1 else expected_seq
    if expected is None:
      if seq is not None or age is not None:
        raise ValueError('NCM display acknowledgement belongs to a different session')
      return False
    if type(seq) is not int or seq != expected:
      raise ValueError(f'NCM display acknowledgement mismatch: sent={expected}, received={seq}')
    if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
      raise ValueError('Invalid NCM display receipt age')
    self.fresh_for = max(0., .5 - age)
    return self.fresh_for > 0


class StatePoll:
  """Display-only STATE replies must not stop 10 Hz HUD writes for 500 ms."""
  def __init__(self, client, clock):
    self.client, self.clock = client, clock
    self.pending = None
    self.next_request = 0.

  def poll(self, receipt):
    now = self.clock()
    if self.pending is None and now >= self.next_request:
      seq = self.client._next_seq()
      self.client.t.send(Msg.STATE_REQ, seq, [b'{}'], timeout=.012)
      # Subsequent HUD messages must not change the receipt expected for this
      # ordered request. Only one outstanding STATE is allowed.
      self.pending = (seq, receipt.last_seq, now)
      self.next_request = now + .25
    if self.pending is None:
      return None
    seq, expected, started = self.pending
    if now - started >= .5:
      raise TimeoutError('NCM display STATE response deadline expired')
    try:
      msg = self.client.t.recv(timeout=.001)
    except LinkTimeout:
      return None  # Stream framing retains partial replies across polls.
    if msg.msg_type != Msg.STATE_RESP or msg.seq != seq:
      raise ValueError('Unexpected NCM display STATE response')
    state = decode_json(bytes(msg.payload), 65536)
    received = receipt.check(state, expected)
    self.pending = None
    self.client.last_state = state
    return received, started, self.clock()


def send_session(client, publisher, running, clock=time.monotonic, sleep=time.sleep):
  peer = client.hello(timeout=3)
  if peer.get(DISPLAY_CAPABILITY) is not True or peer.get(CAPABILITY) is not True:
    raise ValueError('Install the explicitly selected NEXO NCM display-only host first')
  receipt = HudReceipt(client.t, peer.get(ACK_CAPABILITY) is True)
  sender = SimpleNamespace(t=receipt, _next_seq=client._next_seq, last_state=None)
  received, checked_at = False, float('-inf')
  poller = StatePoll(client, clock)
  last_peer_rx = None
  while running():
    result = poller.poll(receipt)
    if result is not None:
      received, checked_at, last_peer_rx = result
      publisher.status['state_rtt_ms'] = (last_peer_rx - checked_at) * 1000
    sender.last_state = getattr(client, 'last_state', None)
    publisher.send(sender)
    publish('connected', last_peer_rx_mono=last_peer_rx, hello_complete=True, interface=getattr(client.t, 'interface', ''),
            peer=getattr(client, 'last_state', None), hud_ack_supported=receipt.supported,
            hud_rx_confirmed=received and clock() - checked_at < receipt.fresh_for, **publisher.status)
    if hasattr(publisher, 'recover_snapshot'):
      publisher.recover_snapshot(clock())
    sleep(.02)


def connect_display_ncm(timeout=5.0):
  """HUD-only unprivileged TCP connection over the already provisioned NCM link.

  AGNOS manager children do not have CAP_NET_RAW; SO_BINDTODEVICE fails with
  EPERM there. Never relax the inference transport: this display-only client
  instead pins its source IP and checks the unique USB gadget, carrier, exact
  /24 address and direct route both before and after TCP connect.
  """
  import socket

  iface = ncm.comma_interface()
  ncm.require_usb_route(iface)
  index = socket.if_nametoindex(iface)
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  try:
    sock.bind((ncm.COMMA_ADDR, 0))
    sock.settimeout(timeout)
    sock.connect((ncm.JETSON_ADDR, ncm.PORT))
    if socket.if_nametoindex(iface) != index or ncm.comma_interface() != iface:
      raise ncm.LinkError('Display NCM gadget changed during connection')
    ncm.require_usb_route(iface)
    if sock.getsockname()[0] != ncm.COMMA_ADDR or sock.getpeername()[0] != ncm.JETSON_ADDR:
      raise ncm.LinkError('Display NCM socket endpoints do not match the provisioned USB pair')
    transport = ncm.JetlinkNcmTcpTransport(sock)
    transport.interface = iface
    return transport
  except Exception:
    sock.close()
    raise


def main():
  from openpilot.common.params import Params
  from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
  params = Params()
  def running():
    return requested(params)
  failures = 0
  while running():
    from openpilot.tools.jetson.process import children_reaping
    if children_reaping():
      publish('waiting_child_reap', error='Optional display child has not exited; no replacement started')
      time.sleep(1)
      continue
    started = time.monotonic()
    client = publisher = None
    try:
      client = JetlinkClient(connect_display_ncm(), name='nexo-ncm-display')
      publisher = DisplayPublisher(standalone=True)
      send_session(client, publisher, running)
    except Exception as error:
      log.warning('NCM display retry: %s', error)
      failures = min(5, failures + 1) if time.monotonic() - started < 30 else 1
      detail = str(error)
      domain = ('usb_ncm_preflight' if client is None and any(word in detail for word in
                ('carrier', 'gadget', 'interface', 'direct route', 'USB-NCM')) else
                'protocol' if isinstance(error, ValueError) else 'tcp')
      publish('retrying', error=detail[:400], error_domain=domain, reconnect_failures=failures,
              reconnect_delay_s=hud_reconnect_delay(failures),
              wire_send=getattr(client.t, 'last_send', None) if client is not None else None,
              **(publisher.status if publisher is not None else {}))
    finally:
      if publisher is not None:
        publisher.close()
      if client is not None:
        client.close()
    time.sleep(hud_reconnect_delay(failures))
  publish('disabled')


if __name__ == '__main__':
  main()
