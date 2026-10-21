"""Explicit NCM HUD-only sessions. No engine, model upload, or inference requests."""
import logging
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
from openpilot.tools.jetson.state import RUNTIME, SNAPSHOT, atomic_json, decode_json, read_fresh

OPT_IN = Path('/data/nexo_jetson_hud_ncm')
STATUS = RUNTIME / 'ncm-display.json'
DISPLAY_CAPABILITY = 'nexo_display_only_v1'
log = logging.getLogger(__name__)


def requested(params):
  # Separate explicit display opt-in. Never touch the inference enable file or
  # compete with the old display-only USB gadget owner.
  return OPT_IN.is_file() and not enabled() and not params.get_bool('NexoJetsonUsb')


def publish(state, **values):
  atomic_json(STATUS, {'updated': time.monotonic(), 'state': state, 'transport': 'ncm-tcp',
                       'inference_enabled': False, **values})


class DisplaySession:
  def __init__(self, transport, navi):
    self.t, self.navi = transport, navi
    self.last_seq, self.hello = -1, False
    self.last_hud = None
    SNAPSHOT.unlink(missing_ok=True)

  def handle(self, msg):
    if msg.seq <= self.last_seq:
      raise ValueError('Non-increasing display sequence')
    self.last_seq = msg.seq
    if msg.msg_type == Msg.HELLO_REQ:
      request = decode_json(bytes(msg.payload), 1024)
      if request.get('client', {}).get('name') != 'nexo-ncm-display':
        raise ValueError('This host is display-only; inference clients are refused')
      self.hello = True
      self.t.send_json(Msg.HELLO_RESP, msg.seq, {'protocol': VERSION, CAPABILITY: True,
                       DISPLAY_CAPABILITY: True, 'carrot_hud_v1': True, 'carrot_navi_v1': True})
      return
    if not self.hello:
      raise ValueError('Display HELLO required')
    if msg.msg_type == Msg.HUD:
      value = validate_snapshot(decode_json(bytes(msg.payload), HUD_LIMIT))
      if not isinstance(value.get('session'), str) or len(value['session']) != 32:
        raise ValueError('Invalid HUD generation')
      value['updated'] = self.last_hud = time.monotonic()
      atomic_json(SNAPSHOT, value)
    elif msg.msg_type == Msg.NAVI_MEDIA:
      self.navi.send(msg.payload)
    elif msg.msg_type == Msg.STATE_REQ:
      self.t.send_json(Msg.STATE_RESP, msg.seq, {'display_only': True, 'engine': None,
                       'telemetry': {'carrot_hud_connected': read_fresh(RUNTIME / 'hud-status.json', 2) is not None,
                                     'hud_data_received': self.last_hud is not None
                                     and time.monotonic() - self.last_hud < .5}})
    else:
      # Includes ENGINE, INFER, upload, Wi-Fi configuration and shutdown.
      raise ValueError(f'Operation forbidden on display-only host: {msg.msg_type}')

  def close(self):
    try:
      self.t.close()
      self.navi.close()
    finally:
      SNAPSHOT.unlink(missing_ok=True)


def host_main():
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
        while True:
          try:
            session.handle(transport.recv(.1))
          except LinkTimeout:
            continue
          publish('connected', last_hud_rx=session.last_hud)
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


def send_session(client, publisher, running, clock=time.monotonic, sleep=time.sleep):
  peer = client.hello(timeout=3)
  if peer.get(DISPLAY_CAPABILITY) is not True or peer.get(CAPABILITY) is not True:
    raise ValueError('Install the explicitly selected NEXO NCM display-only host first')
  next_state = 0.
  while running():
    now = clock()
    if now >= next_state:
      client.last_state = client.state(timeout=.5)
      next_state = now + .25
    publisher.send(client)
    publish('connected', interface=getattr(client.t, 'interface', ''),
            peer=client.last_state, **publisher.status)
    if publisher.process.poll() is not None:
      raise RuntimeError('HUD snapshot producer stopped')
    sleep(.02)


def main():
  from openpilot.common.params import Params
  from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
  params = Params()
  def running():
    return requested(params)
  while running():
    client = publisher = None
    try:
      client = JetlinkClient(ncm.JetlinkNcmTcpTransport.connect(), name='nexo-ncm-display')
      publisher = DisplayPublisher()
      send_session(client, publisher, running)
    except Exception as error:
      log.warning('NCM display retry: %s', error)
      publish('retrying', error=str(error)[:400])
    finally:
      if publisher is not None:
        publisher.close()
      if client is not None:
        client.close()
    time.sleep(2)
  publish('disabled')


if __name__ == '__main__':
  main()
