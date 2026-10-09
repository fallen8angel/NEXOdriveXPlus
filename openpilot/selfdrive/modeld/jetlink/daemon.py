"""NEXO Jetlink USB owner and modeld inference bridge.

This is a narrow Jetson-only adaptation of Carrot jetlinkd. It owns the JLNK
FunctionFS gadget, verifies the exact NEXO driving model requested by modeld,
and serves bounded local inference requests. It never publishes CAN, changes
vehicle control parameters, or substitutes a different driving model.
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
from openpilot.selfdrive.modeld.jetlink import SOCKET, STATUS, SPEC_FILE, GADGET, FFS_MOUNT, enabled
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.link import REQUEST, REPLY, PacketReader, send_parts
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec
from openpilot.selfdrive.modeld.jetlink.transport import JetlinkFfsTransport

SCRIPT = Path(__file__).with_name('setup_gadget.sh')


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


def setup_gadget():
  subprocess.run(['sudo', '-n', 'bash', str(SCRIPT)], check=True, timeout=15, capture_output=True)


def serve_modeld(listener, client, spec: ModelSpec):
  last_publish = 0.
  last_infer = None
  telemetry_updated = time.monotonic()

  def report():
    nonlocal last_publish
    now = time.monotonic()
    if now - last_publish >= 1:
      publish('ready', spec, peer=client.last_state or {}, telemetry_updated=telemetry_updated,
              last_infer_monotonic=last_infer)
      last_publish = now

  while enabled():
    report()
    try:
      connection, _ = listener.accept()
    except TimeoutError:
      # A failed STATE exchange must expire this optional session and enter
      # the existing retry path, rather than indefinitely publishing ready.
      client.last_state = client.state(timeout=.5)
      telemetry_updated = time.monotonic()
      continue

    with connection:
      connection.settimeout(.5)
      send_parts(connection, json.dumps({'spec': spec.to_dict()}, separators=(',', ':')).encode())
      reader = PacketReader(REQUEST.size + spec.warped_nbytes + spec.packed_nbytes)
      while enabled():
        try:
          request = reader.receive(connection)
        except TimeoutError:
          report()
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
        report()


def main():
  Path(SOCKET).unlink(missing_ok=True)
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(SOCKET)
  os.chmod(SOCKET, 0o600)
  listener.listen(1)
  listener.settimeout(.25)
  publish('waiting_model_contract')

  try:
    while enabled():
      spec = load_spec()
      if spec is None:
        publish('waiting_model_contract')
        time.sleep(.25)
        continue

      try:
        if Params().get_bool('NexoJetsonUsb'):
          publish('conflict', spec, error='NexoJetsonUsb must be OFF while NEXO Jetlink inference is enabled')
          time.sleep(1)
          continue
      except Exception:
        pass

      transport = None
      client = None
      try:
        publish('connecting', spec)
        setup_gadget()
        udcs = list(Path('/sys/class/udc').iterdir())
        if not udcs:
          raise RuntimeError('no USB device controller')
        transport = JetlinkFfsTransport(FFS_MOUNT, GADGET, udc=udcs[0].name)
        client = JetlinkClient(transport)
        peer = client.hello()
        client.ensure_engine(spec)
        # Two reset warmups prove that transport, backend and exact model
        # contract work before modeld is allowed to join the Jetson path.
        warm_images = np.zeros(spec.warped_shape, np.uint8)
        warm_packed = np.zeros(spec.packed_nelem, np.float32)
        for frame in range(2):
          client.infer(warm_images, warm_packed, frame, reset=True)
        client.last_state = client.state(timeout=.5)
        cloudlog.warning('NEXO Jetlink ready: native model %s peer=%s', spec.sha256, peer)
        serve_modeld(listener, client, spec)
      except (ConnectionError, BrokenPipeError, TimeoutError, ValueError, OSError, RuntimeError) as exc:
        cloudlog.warning('NEXO Jetlink retry: %s', exc)
        publish('retrying', spec, error=str(exc)[:300])
      except Exception as exc:
        cloudlog.exception('NEXO Jetlink unexpected failure')
        publish('retrying', spec, error=str(exc)[:300])
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
