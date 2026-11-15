"""Startup classification and resident-only preparation; inference deadlines stay unchanged."""
from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout


def initial_connection_wait(error, *, elapsed, ready_once, stage):
  no_reader = isinstance(error, LinkError) and str(error).startswith((
    'gadget write had no reader for ', 'gadget write had no reader before deadline;'))
  return (not ready_once and 0 <= elapsed < 120 and stage in ('transport', 'hello')
          and (isinstance(error, (TimeoutError, LinkTimeout, ConnectionRefusedError)) or no_reader))
RESIDENT_CAPABILITY = 'nexo_resident_engine_v1'


def prepare_resident_engine(client, wanted, peer, *, connected, progress, timeout=900., clock=None, sleep=None, idle=None):
  """Carrot dcd3392/b128559: retain the link and refresh before/after STATE.

  NEXO is stricter: preparation never starts a build or switches the resident
  model, even offroad. Explicit prepare_engine owns those operations separately.
  """
  import time
  from openpilot.selfdrive.modeld.jetlink.client import EngineMissing
  from openpilot.tools.jetson.transport.base import LinkError, LinkTimeout
  clock = time.monotonic if clock is None else clock
  sleep = time.sleep if sleep is None else sleep
  if not isinstance(peer, dict) or peer.get(RESIDENT_CAPABILITY) is not True:
    raise LinkError('Protected resident-only Jetson host required; no engine setup request sent')
  deadline = clock() + timeout
  current = peer
  reported_at = float('-inf')
  def maintain():
    nonlocal reported_at
    if idle is not None:
      idle()  # The same owner sends HUD, with no inference reply outstanding.
    if clock() - reported_at >= 1.:
      progress(preparation={'stage': 'resident-wait', 'message': '모델 준비 대기 · HUD 연결 유지 · 별도 Offroad 엔진 준비 필요'})
      reported_at = clock()
  def report():
    progress(preparation={'stage': 'resident-wait', 'message': '모델 준비 대기 · 연결 유지 · 별도 Offroad 엔진 준비 필요'})
  while connected():
    if clock() >= deadline:
      raise LinkTimeout('Resident engine preparation wait exceeded its time budget')
    report()
    if current.get('loaded') == wanted.sha256:
      try:
        return client.ensure_engine(wanted, timeout=min(3., max(.01, deadline - clock())),
                                    progress=report, resident_only=True)
      except EngineMissing:
        pass  # Unloaded since STATE/HELLO: keep this connection, never build.
    report()
    current = client.state(timeout=min(2., max(.01, deadline - clock())),
                           **({'progress': maintain} if idle is not None else {}))
    report()  # b128559: a slow successful STATE read must refresh its lease.
    if not isinstance(current, dict) or 'loaded' not in current:
      raise LinkError('Jetson resident-model STATE response is invalid')
    if current.get('engine_state') == 'failed':
      raise LinkError('Jetson engine preparation failed: ' + str(current.get('detail', ''))[:200])
    preparation = current.get('nexo_engine_preparation') or {}
    if not isinstance(preparation, dict):
      raise LinkError('Jetson engine preparation metadata is invalid')
    if preparation.get('sha256') == wanted.sha256 and preparation.get('state') == 'failed':
      raise LinkError('Jetson native model preparation failed: ' + str(preparation.get('detail', ''))[:200])
    client.last_state = current
    if idle is None:
      sleep(.5)
    else:
      until = min(clock() + .5, deadline)
      while connected() and clock() < until:
        maintain()
        sleep(min(.05, max(0., until - clock())))
  raise LinkError('Jetlink preparation cancelled or disconnected')
