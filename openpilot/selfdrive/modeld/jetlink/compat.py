"""Session-local negotiation and bounded recovery; never changes model policy."""
from openpilot.selfdrive.modeld.jetlink import protocol as V2, protocol_v3 as V3


def wire_protocol(version):
  if type(version) is not int or version not in (2, 3):
    raise ValueError('Jetlink supports only protocol 2 or 3')
  return V2 if version == 2 else V3


class ProtocolChoice:
  """A rejected HELLO requires a fresh stream, never an in-band header switch.

  Prefer deployed v2, with v3 on the next fresh stream; a successful session
  keeps its version. Engine, SHA and inference failures do not change it.
  """
  def __init__(self):
    self.version = 2

  def handshake_failed(self):
    self.version = 5 - self.version


class Reconnect:
  def __init__(self):
    self.attempts = 0
    self.failures = 0
    self.last_error = ''
    self.ready_since = None

  def attempt(self):
    self.attempts += 1
    self.ready_since = None

  def failed(self, error, now):
    # Only sustained healthy service resets escalation, not a successful HELLO.
    if self.ready_since is not None and now - self.ready_since >= 30:
      self.failures = 0
    self.failures += 1
    self.last_error = str(error)[:500]
    return min(30., 2. ** min(self.failures, 5))

  def status(self):
    return {'reconnect_count': max(0, self.attempts - 1), 'last_connection_error': self.last_error}
