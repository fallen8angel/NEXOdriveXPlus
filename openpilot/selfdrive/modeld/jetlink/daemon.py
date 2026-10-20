"""NEXO Jetlink USB owner and modeld inference bridge.

This is a narrow Jetson-only adaptation of Carrot jetlinkd. It owns the JLNK
FunctionFS gadget, verifies the exact NEXO driving model requested by modeld,
and serves bounded local inference requests. It never publishes CAN, changes
vehicle control parameters, or substitutes a different driving model.
"""
from __future__ import annotations

import json
from functools import partial
import os
from pathlib import Path
import socket
import subprocess
import time

import numpy as np

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.modeld.jetlink import SOCKET, STATUS, SPEC_FILE, GADGET, FFS_MOUNT, enabled
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.link import REQUEST, REPLY, PacketReader, send_parts
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec
from openpilot.selfdrive.modeld.jetlink.transport import JetlinkFfsTransport
from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport, transport_mode
from openpilot.selfdrive.modeld.jetlink.display import CAPABILITY, DisplayPublisher
from openpilot.selfdrive.modeld.jetlink.startup import initial_connection_wait

SCRIPT = Path(__file__).with_name('setup_gadget.sh')
GADGET_STATUS = Path('/dev/shm/nexo-jetlink-gadget')


def load_spec() -> ModelSpec | None:
  try:
    return ModelSpec.from_dict(json.loads(SPEC_FILE.read_text()))
  except (OSError, ValueError, KeyError, TypeError):
    return None


def publish(state: str, spec: ModelSpec | None = None, **extra):
  record = dict(state=state, updated=time.monotonic(), model='NEXO native driving_supercombo',
                sha256=spec.sha256 if spec is not None else '', **extra)
  tmp = STATUS.with_suffix('.tmp')
  try:
    tmp.write_text(json.dumps(record, separators=(',', ':')))
    os.replace(tmp, STATUS)
  except OSError:
    pass


def setup_gadget(takeover_display: bool = False):
  command = ['sudo', '-n', 'bash', str(SCRIPT)]
  env = os.environ.copy()
  if takeover_display:
    # The caller has already verified NexoJetsonUsb=False. This authorizes the
    # setup script to release only a stale NEXO display-only gadget, never an
    # unrelated AGNOS/adb/network USB owner.
    env['JETLINK_TAKEOVER_NEXO_DISPLAY'] = '1'
  try:
    result = subprocess.run(command, check=False, timeout=15, capture_output=True, text=True, env=env)
  except subprocess.TimeoutExpired as exc:
    raise RuntimeError('setup_gadget.sh timed out after 15 seconds') from exc

  if result.returncode == 0:
    return

  details = []
  for value in (result.stderr, result.stdout):
    value = (value or '').strip()
    if value:
      details.append(value)

  if not details:
    try:
      status = GADGET_STATUS.read_text().strip()
      if status:
        details.append(status)
    except OSError:
      pass

  detail = ' | '.join(details) if details else 'no stderr/stdout; inspect USB gadget prerequisites'
  # Keep the shared status small enough for the 7000 page while preserving the
  # script's actual failure reason (sudo, UDC ownership, configfs, FunctionFS).
  raise RuntimeError(f'setup_gadget.sh exit {result.returncode}: {detail}'[:500])


def serve_modeld(listener, client, spec: ModelSpec, peer=None):
  publisher = None
  display = {'hud_enabled': False, 'display_error': 'Jetson NEXO HUD service install required'}
  if isinstance(peer, dict) and peer.get(CAPABILITY) is True and peer.get('carrot_hud_v1') is True:
    try:
      publisher = DisplayPublisher(navi=peer.get('carrot_navi_v1') is True)
      display = publisher.status
    except Exception as exc:
      display['display_error'] = f'HUD worker unavailable: {type(exc).__name__}'
      cloudlog.warning('NEXO optional HUD unavailable: %s', exc)
  try:
    _serve_modeld(listener, client, spec, publisher, display)
  finally:
    if publisher is not None:
      try:
        publisher.close()
      except Exception:
        cloudlog.exception('NEXO HUD worker cleanup failed')


def _serve_modeld(listener, client, spec: ModelSpec, publisher=None, display=None):
  last_publish = 0.
  last_infer = None
  telemetry_updated = time.monotonic()

  def idle_state():
    nonlocal telemetry_updated
    # A local modeld socket may stay open while no frames arrive. Probe only
    # after its idle timeout, never ahead of a queued inference or per frame.
    # Failure must leave ready and reach the existing transport retry/cleanup.
    if time.monotonic() - telemetry_updated >= 1:
      client.last_state = client.state(timeout=.5)
      telemetry_updated = time.monotonic()

  def report():
    nonlocal last_publish
    now = time.monotonic()
    if now - last_publish >= 1:
      publish('ready', spec, peer=client.last_state or {}, telemetry_updated=telemetry_updated,
              last_infer_monotonic=last_infer, transport=getattr(client, 'transport_mode', 'usb'),
              interface=getattr(getattr(client, 't', None), 'interface', ''), **(display or {}))
      last_publish = now

  while enabled():
    report()
    try:
      connection, _ = listener.accept()
    except TimeoutError:
      # A failed STATE exchange must expire this optional session and enter
      # the existing retry path, rather than indefinitely publishing ready.
      idle_state()
      if publisher is not None:
        publisher.send(client)
      continue

    with connection:
      connection.settimeout(.5)
      send_parts(connection, json.dumps({'spec': spec.to_dict()}, separators=(',', ':')).encode())
      reader = PacketReader(REQUEST.size + spec.warped_nbytes + spec.packed_nbytes)
      while enabled():
        try:
          request = reader.receive(connection)
        except TimeoutError:
          idle_state()
          report()
          if publisher is not None:
            publisher.send(client)
          continue
        if len(request) != REQUEST.size + spec.warped_nbytes + spec.packed_nbytes:
          raise ValueError('invalid local inference request size')
        frame, reset, source_sof = REQUEST.unpack_from(request)
        if reset not in (0, 1):
          raise ValueError('invalid reset flag')
        images = np.frombuffer(request, np.uint8, spec.warped_nbytes, REQUEST.size).reshape(spec.warped_shape)
        packed = np.frombuffer(request, np.float32, spec.packed_nelem, REQUEST.size + spec.warped_nbytes)
        if not np.all(np.isfinite(packed)):
          raise ValueError('non-finite local model context')
        previous_telemetry = client.last_state
        output = client.infer(images, packed, frame, bool(reset), want_state=(frame % 20 == 0))
        last_infer = time.monotonic()
        if frame % 20 == 0 and isinstance(client.last_state, dict) and client.last_state is not previous_telemetry:
          telemetry_updated = last_infer
        send_parts(connection, REPLY.pack(frame, *client.last_timings), output)
        # Never put JSON/preview generation or display USB transfers before
        # the inference result. The same owner serializes bounded display tail.
        if publisher is not None:
          publisher.send(client)
        report()


def connect_transport(mode):
  if mode == 'ncm-tcp':
    return JetlinkNcmTcpTransport.connect()
  if mode != 'usb':
    raise ValueError('unsupported Jetlink transport')
  setup_gadget(takeover_display=True)
  udcs = list(Path('/sys/class/udc').iterdir())
  if not udcs:
    raise RuntimeError('no USB device controller')
  return JetlinkFfsTransport(FFS_MOUNT, GADGET, udc=udcs[0].name)


def main():
  Path(SOCKET).unlink(missing_ok=True)
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(SOCKET)
  os.chmod(SOCKET, 0o600)
  listener.listen(1)
  listener.settimeout(.25)
  publish('waiting_model_contract')
  initial_started = time.monotonic()
  ready_once = False

  try:
    while enabled():
      spec = load_spec()
      if spec is None:
        publish('waiting_model_contract')
        time.sleep(.25)
        continue

      # USB ownership must be known before setup is allowed to tear down the
      # legacy NEXO display-only gadget. If Params cannot be read, retry without
      # touching USB instead of guessing that display mode is off.
      try:
        display_usb_enabled = Params().get_bool('NexoJetsonUsb')
      except Exception as exc:
        publish('retrying', spec, error=f'cannot verify NexoJetsonUsb state: {type(exc).__name__}: {exc}'[:300])
        time.sleep(1)
        continue
      if display_usb_enabled:
        publish('conflict', spec, error='NexoJetsonUsb must be OFF while NEXO Jetlink inference is enabled')
        time.sleep(1)
        continue

      transport = None
      client = None
      mode = 'unknown'
      stage = 'transport'
      peer = None
      try:
        # The default is still dedicated USB FunctionFS. Only an explicit
        # offroad-provisioned ncm-tcp selection uses AGNOS g1's existing NCM
        # interface; never steal the ADB/NCM gadget or fall back to Wi-Fi.
        mode = transport_mode()
        publish('connecting', spec, transport=mode)
        transport = connect_transport(mode)
        client = JetlinkClient(transport)
        client.transport_mode = mode
        stage = 'hello'
        progress = partial(publish, 'connecting', spec, transport=mode, stage=stage,
                           interface=getattr(transport, 'interface', ''), usb_checked=mode == 'ncm-tcp')
        peer = client.hello(progress=progress)
        stage = 'model'
        progress = partial(publish, 'loading', spec, transport=mode, stage=stage,
                           interface=getattr(transport, 'interface', ''), usb_checked=True, peer=peer)
        progress()
        client.ensure_engine(spec, progress=progress)
        # Two reset warmups prove that transport, backend and exact model
        # contract work before modeld is allowed to join the Jetson path.
        warm_images = np.zeros(spec.warped_shape, np.uint8)
        warm_packed = np.zeros(spec.packed_nelem, np.float32)
        for frame in range(2):
          client.infer(warm_images, warm_packed, frame, reset=True)
        client.last_state = client.state(timeout=.5)
        ready_once = True
        stage = 'session'
        cloudlog.warning('NEXO Jetlink ready: native model %s peer=%s', spec.sha256, peer)
        serve_modeld(listener, client, spec, peer)
      except (ConnectionError, BrokenPipeError, TimeoutError, ValueError, OSError, RuntimeError) as exc:
        cloudlog.warning('NEXO Jetlink retry: %s', exc)
        waiting = initial_connection_wait(exc, elapsed=time.monotonic() - initial_started, ready_once=ready_once, stage=stage)
        publish('connecting' if waiting else 'retrying', spec, transport=mode, stage=stage,
                **({'wait_reason': str(exc)[:300]} if waiting else {'error': str(exc)[:300]}))
      except Exception as exc:
        cloudlog.exception('NEXO Jetlink unexpected failure')
        publish('retrying', spec, transport=mode, error=str(exc)[:300])
      finally:
        if client is not None:
          try:
            client.close()
          except Exception:
            pass
        elif transport is not None:
          try:
            transport.close()
          except Exception:
            pass
      if enabled():
        time.sleep(2)
  finally:
    listener.close()
    Path(SOCKET).unlink(missing_ok=True)
    publish('stopped', load_spec())


if __name__ == '__main__':
  main()
