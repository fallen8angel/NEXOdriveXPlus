"""Opt-in bench-only USB receiver diagnostic, not a traffic signal decoder.

Public V2X2MAP bridge wire reference (MIT):
https://github.com/pit711/v2x2map/blob/main/bridge/its_g5_bridge.py
ITS5 framing is documented for the public bridge, not confirmed for INT-BETA.
No CAN, modeld, controls, HUD, network, MQTT, or persistent capture integration.
Run on a disconnected bench, not while driving. No automatic service startup.
"""
import argparse
import json
import struct
import time

MAGIC = b"ITS5"
HEADER = struct.Struct("<4sIIH")
MAX_PAYLOAD = 4096
ESPRESSIF_VID = 0x303A


class FrameCounter:
  """Count structurally plausible public-bridge frames; never decode or retain identities."""
  def __init__(self):
    self.buffer = bytearray()
    self.frames = 0
    self.rejected = 0
    self.bytes_received = 0

  def feed(self, chunk):
    self.bytes_received += len(chunk)
    self.buffer.extend(chunk)
    while True:
      start = self.buffer.find(MAGIC)
      if start < 0:
        if len(self.buffer) > 3:
          del self.buffer[:-3]
        return
      if start:
        del self.buffer[:start]
      if len(self.buffer) < HEADER.size:
        return
      _, _, usec, length = HEADER.unpack_from(self.buffer)
      if not 1 <= length <= MAX_PAYLOAD or usec >= 1_000_000:
        self.rejected += 1
        del self.buffer[:1]
        continue
      end = HEADER.size + length
      if len(self.buffer) < end:
        return
      del self.buffer[:end]
      self.frames += 1


def self_test():
  frame = HEADER.pack(MAGIC, 1, 2, 3) + b"abc"
  for cut in range(len(frame) + 1):
    counter = FrameCounter()
    counter.feed(b"noise" + frame[:cut])
    counter.feed(frame[cut:] + frame)
    if counter.frames != 2 or counter.buffer:
      raise AssertionError("split frame")
  counter = FrameCounter()
  counter.feed(HEADER.pack(MAGIC, 1, 0, MAX_PAYLOAD + 1) + frame)
  if counter.frames != 1 or counter.rejected != 1:
    raise AssertionError("invalid length recovery")
  counter.feed(b"x" * 10000)
  if len(counter.buffer) > 3:
    raise AssertionError("buffer bound")
  counter.feed(HEADER.pack(MAGIC, 1, 1_000_000, 3) + frame)
  if counter.frames != 2 or counter.rejected != 2:
    raise AssertionError("invalid timestamp recovery")
  print(json.dumps({"state": "self_test_passed"}))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument("--list", action="store_true", help="list native Espressif USB serial devices")
  mode.add_argument("--port", help="explicit native USB device, e.g. /dev/ttyACM0")
  mode.add_argument("--self-test", action="store_true")
  parser.add_argument("--seconds", type=int, default=10, choices=range(1, 61), metavar="1..60")
  args = parser.parse_args()
  if args.self_test:
    self_test()
    return 0
  try:
    import serial
    from serial.tools import list_ports
  except ImportError:
    print(json.dumps({"state": "dependency_missing", "dependency": "pyserial"}))
    return 2
  ports = [p for p in list_ports.comports() if p.vid == ESPRESSIF_VID]
  if args.list:
    print(json.dumps({"state": "listed", "ports": [p.device for p in ports]}))
    return 0
  if args.port not in {p.device for p in ports}:
    print(json.dumps({"state": "native_espressif_port_not_found"}))
    return 2
  counter = FrameCounter()
  device = serial.Serial(port=None, baudrate=115200, timeout=0.2, exclusive=True)
  device.port = args.port
  device.dtr = False
  device.rts = False
  started = time.monotonic()
  try:
    device.open()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
      counter.feed(device.read(4096))
  except (OSError, serial.SerialException) as exc:
    print(json.dumps({"state": "serial_error", "error_type": type(exc).__name__}))
    return 2
  finally:
    device.close()
  print(json.dumps({
    "state": "public_framing_detected" if counter.frames else "no_public_frames",
    "bytes_received": counter.bytes_received,
    "public_frame_candidates": counter.frames,
    "rejected_headers": counter.rejected,
    "elapsed_seconds": round(time.monotonic() - started, 2),
    "int_beta_verified": False,
    "signal_verified": False,
    "hud_enabled": False,
  }))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
