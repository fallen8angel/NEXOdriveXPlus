"""NEXO Jetlink USB owner and modeld inference bridge.

This is a deliberately narrow Jetson-only adaptation of Carrot jetlinkd.  It
owns the real JLNK FunctionFS gadget, verifies the pinned Cinque v2 engine, and
serves bounded local inference requests from modeld.  It never publishes CAN,
changes Params, or participates in vehicle control.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time

import numpy as np

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.modeld.jetlink import SOCKET, STATUS, GADGET, FFS_MOUNT, enabled
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.link import SPEC, REQUEST, REPLY, PacketReader, send_parts
from openpilot.selfdrive.modeld.jetlink.transport import JetlinkFfsTransport

SCRIPT = Path(__file__).with_name('setup_gadget.sh')


def publish(state: str, **extra):
  record = dict(state=state, updated=time.monotonic(), model='Cinque v2', sha256=SPEC.sha256, **extra)
  tmp = STATUS.with_suffix('.tmp')
  try:
    tmp.write_text(json.dumps(record, separators=(',', ':')))
    os.replace(tmp, STATUS)
  except OSError:
    pass


def setup_gadget():
  subprocess.run(['sudo', '-n', 'bash', str(SCRIPT)], check=True, timeout=15, capture_output=True)


def serve_modeld(listener, client):
  while enabled():
    publish('ready', peer=client.last_state or {})
    try:
      connection, _ = listener.accept()
    except TimeoutError:
      try:
        client.last_state = client.state(timeout=.5)
      except Exception:
        pass
      continue

    with connection:
      connection.settimeout(.5)
      send_parts(connection, json.dumps({'spec': SPEC.to_dict()}).encode())
      reader = PacketReader(REQUEST.size + SPEC.warped_nbytes + SPEC.packed_nbytes)
      while enabled():
        try:
          request = reader.receive(connection)
        except TimeoutError:
          continue
        if len(request) != REQUEST.size + SPEC.warped_nbytes + SPEC.packed_nbytes:
          raise ValueError('invalid local inference request size')
        frame, reset, source_sof = REQUEST.unpack_from(request)
        if reset not in (0, 1):
          raise ValueError('invalid reset flag')
        images = np.frombuffer(request, np.uint8, SPEC.warped_nbytes, REQUEST.size).reshape(SPEC.warped_shape)
        packed = np.frombuffer(request, np.float32, SPEC.packed_nelem, REQUEST.size + SPEC.warped_nbytes)
        if not np.all(np.isfinite(packed)):
          raise ValueError('non-finite local model context')
        output = client.infer(images, packed, frame, bool(reset), want_state=(frame % 20 == 0))
        send_parts(connection, REPLY.pack(frame, *client.last_timings), output)


def main():
  Path(SOCKET).unlink(missing_ok=True)
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(SOCKET)
  os.chmod(SOCKET, 0o600)
  listener.listen(1)
  listener.settimeout(.25)
  publish('starting')

  try:
    while enabled():
      # Current NEXO display USB owns the same UDC.  Never fight it: the user
      # must turn that optional display link off before enabling model offload.
      try:
        if Params().get_bool('NexoJetsonUsb'):
          publish('conflict', error='NexoJetsonUsb must be OFF while NEXO Jetlink inference is enabled')
          time.sleep(1)
          continue
      except Exception:
        pass

      transport = None
      client = None
      try:
        publish('connecting')
        setup_gadget()
        udcs = list(Path('/sys/class/udc').iterdir())
        if not udcs:
          raise RuntimeError('no USB device controller')
        transport = JetlinkFfsTransport(FFS_MOUNT, GADGET, udc=udcs[0].name)
        client = JetlinkClient(transport)
        peer = client.hello()
        client.ensure_engine(SPEC)
        # Verify inference before advertising ready. Every real modeld session
        # starts with reset=True so this warmup never leaks recurrent state.
        warm_images = np.zeros(SPEC.warped_shape, np.uint8)
        warm_packed = np.zeros(SPEC.packed_nelem, np.float32)
        for frame in range(2):
          client.infer(warm_images, warm_packed, frame, reset=True)
        client.last_state = peer
        cloudlog.warning('NEXO Jetlink ready: %s', peer)
        serve_modeld(listener, client)
      except (ConnectionError, BrokenPipeError, TimeoutError, ValueError, OSError, RuntimeError) as exc:
        cloudlog.warning('NEXO Jetlink retry: %s', exc)
        publish('retrying', error=str(exc)[:300])
      except Exception as exc:
        cloudlog.exception('NEXO Jetlink unexpected failure')
        publish('retrying', error=str(exc)[:300])
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
    publish('stopped')


if __name__ == '__main__':
  main()
