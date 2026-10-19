"""One encoded-video subscriber with bounded waits and no retained frame batch."""
import socket
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def single_yolo_instance(path=Path('/tmp/nexo-yolo-direct.lock')):
  import fcntl
  # The service and direct manual entry point use the same lock. Failure to
  # acquire it exits without touching the existing GPU/subscription owner.
  with Path(path).open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield


def close_socket(sock):
  close = getattr(sock, 'close', None)
  if callable(close):
    try:
      close()
    except OSError:
      pass


class EncodedInput:
  def __init__(self, messaging, address, conflate=False, clock=time.monotonic):
    self.messaging, self.address, self.conflate = messaging, address, conflate
    self.clock = clock
    self.sock = None
    self.generation = -1
    self.reconnects = 0
    self.last_error = ''
    self.renew()

  def close(self):
    previous, self.sock = self.sock, None
    close_socket(previous)

  def renew(self):
    self.close()
    self.generation += 1
    # A configured hostname can follow DHCP changes; a fixed IP is never
    # silently replaced by an unauthenticated network discovery result.
    address = socket.gethostbyname(self.address)
    self.sock = self.messaging.sub_sock('roadEncodeData', None, addr=address,
                                        conflate=self.conflate, timeout=1000)
    self.last_packet = self.clock()

  def receive(self):
    try:
      event = self.messaging.recv_one(self.sock)
    except (OSError, RuntimeError) as exc:
      self.last_error = str(exc)[:240]
      time.sleep(.1)
      event = None
    if event is not None:
      self.last_packet = self.clock()
    elif self.clock() - self.last_packet >= 5:
      self.last_error = self.last_error or 'roadEncodeData stalled; subscription renewed'
      self.reconnects += 1
      self.renew()
    return event
