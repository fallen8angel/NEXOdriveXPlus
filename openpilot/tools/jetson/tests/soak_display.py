"""120 s wall-clock TCP/HUD simulation; no vehicle, USB or real panel claims.

Run with CARROT_JETSON pointing at the pinned vendor and --output outside repo.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import psutil

from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet
from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport
from openpilot.selfdrive.modeld.jetlink.protocol import Msg
from openpilot.tools.jetson import ncm_display as display
from openpilot.tools.jetson.ncm_server import transport_class
from openpilot.tools.jetson.state import atomic_json


def run(output, seconds=120):
  assert seconds >= 120
  output.mkdir(parents=True, exist_ok=True)
  display.SNAPSHOT, display.RUNTIME = output / 'hud.json', output
  sys.path.insert(0, str(Path(os.environ['CARROT_JETSON']) / 'third_party/jetlink'))
  errors, recoveries, samples, threads = [], [], [], []
  process = psutil.Process()
  before = {'rss': process.memory_info().rss, 'children': len(process.children()), 'threads': process.num_threads()}
  client = server = thread = None
  stop = threading.Event()
  def close():
    stop.set()
    if client:
      client.close()
    if thread:
      thread.join(2)
      assert not thread.is_alive()
    if server:
      server.close()
  def connect():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    sock = socket.create_connection(listener.getsockname())
    peer, _ = listener.accept()
    listener.close()
    new_client = JetlinkClient(JetlinkNcmTcpTransport(sock), name='nexo-ncm-display')
    new_server = display.DisplaySession(transport_class()(peer), SimpleNamespace(send=lambda raw: None, close=lambda: None))
    stop.clear()
    def serve():
      from jetlink.transport.base import LinkTimeout
      while not stop.is_set():
        try:
          msg = new_server.t.recv(.2)
          new_server.handle(msg)
          if msg.msg_type == Msg.HUD:
            stamp = time.monotonic()
            atomic_json(output / 'hud-status.json', {'updated': stamp, 'last_panel_send_mono': stamp, 'pid': os.getpid()})
        except LinkTimeout:
          continue
        except Exception as exc:
          if not stop.is_set():
            errors.append(type(exc).__name__ + ': ' + str(exc))
            print(errors[-1], flush=True)
          return
    new_thread = threading.Thread(target=serve)
    new_thread.start()
    threads.append(new_thread)
    assert new_client.hello()[display.ACK_CAPABILITY]
    return new_client, new_server, new_thread, display.HudReceipt(new_client.t, True)
  started = time.monotonic()
  client, server, thread, receipt = connect()
  restarted = set()
  count = 0
  try:
    while (elapsed := time.monotonic() - started) < seconds:
      # Two deliberate stream replacements model comma/Jetson session restarts.
      for at in (30, 90):
        if elapsed >= at and at not in restarted:
          recovery_start = time.perf_counter()
          close()
          client, server, thread, receipt = connect()
          recoveries.append({'at_s': at, 'hello_recovery_s': time.perf_counter() - recovery_start})
          restarted.add(at)
      pause = 60 <= elapsed < 61
      now = time.monotonic()
      if not pause:
        value = {'version': 1, 'sent': now, 'events': {}, 'params': {'IsOnroad': 'MQ=='},
                 'mono': {}, 'received': {}, 'valid': {}, 'alive': {}, 'cameras': {}}
        raw = snapshot_packet(json.dumps(value).encode(), 'a' * 32)
        receipt.send(Msg.HUD, client._next_seq(), [raw])
        count += 1
      state = client.state()
      confirmed = receipt.check(state)
      if not pause:
        assert confirmed and state['telemetry']['hud_data_received']
        if recoveries and 'hud_resume_s' not in recoveries[-1]:
          recoveries[-1]['hud_resume_s'] = time.perf_counter() - recovery_start
      samples.append({'at_s': elapsed, 'paused': pause, 'ack': confirmed,
                      'rx_age': state['hud_rx_age'], 'rss': process.memory_info().rss})
      time.sleep(max(0, now + .1 - time.monotonic()))
  finally:
    close()
  after = {'rss': process.memory_info().rss, 'children': len(process.children()), 'threads': process.num_threads()}
  report = {'duration_s': time.monotonic() - started, 'snapshots': count, 'samples': len(samples),
            'errors': errors, 'deliberate_reconnects': recoveries,
            'data_pause_observed': any(row['paused'] and not row['ack'] for row in samples),
            'before': before, 'after': after, 'live_test_threads': sum(t.is_alive() for t in threads),
            'physical_validation': False,
            'limits': 'Loopback TCP, synthetic empty stationary HUD payload, simulated panel-write telemetry; no GPU/USB/pixels/vehicle validation.'}
  (output / 'soak-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
  (output / 'soak-samples.jsonl').write_text('\n'.join(json.dumps(row) for row in samples), encoding='utf-8')
  assert report['duration_s'] >= 120 and count >= 1100 and not errors
  assert report['data_pause_observed'] and report['live_test_threads'] == 0
  assert after['children'] == before['children']
  print(json.dumps(report))


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--output', type=Path, required=True)
  run(parser.parse_args().output)
