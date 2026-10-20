"""Presentation-only startup classification; no retry/deadline policy changes."""
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout


def initial_connection_wait(error, *, elapsed, ready_once, stage):
  no_reader = isinstance(error, LinkError) and str(error).startswith((
    'gadget write had no reader for ', 'gadget write had no reader before deadline;'))
  return (not ready_once and 0 <= elapsed < 120 and stage in ('transport', 'hello')
          and (isinstance(error, (TimeoutError, LinkTimeout, ConnectionRefusedError)) or no_reader))
