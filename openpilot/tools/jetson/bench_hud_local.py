"""Local-only encoder benchmark and bounded TCP soak; never connects to a device.

Run with PYTHONPATH including the repository and opendbc_repo. Requires ffmpeg,
psutil and the existing pinned CARROT_JETSON checkout. Results are host metrics,
not measurements of Jetson CPU or physical USB performance.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import psutil


def encoder_benchmark():
  sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'selfdrive/carrot/cluster'))
  from cluster_h264_pipeline import H264UsbPipeline
  results = []
  # Fixed, asymmetric RGBA input with unchanged production encoder command.
  frame = (bytes((230, 30, 40, 255)) * 231 + bytes((30, 220, 80, 255)) * 231) * 1920
  for threads in (0, 1, 1, 0):
    pipeline = H264UsbPipeline(None, 462, 1920, 1, 10, '2333k', 1, 'ffmpeg', '',
      'ffmpeg', 'libx264', '', 'nv12', 0, 'vbr-cfr', False, 0, False, False, '', False,
      ffmpeg_threads=threads)
    samples, stop = [], threading.Event()
    started = time.perf_counter()
    process = subprocess.Popen(pipeline._ffmpeg_command('ffmpeg', 'libx264', 'h264'),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def monitor(process=process, stop=stop, samples=samples):
      child = psutil.Process(process.pid)
      while not stop.wait(.01):
        try:
          cpu = child.cpu_times()
          samples.append((child.memory_info().rss, cpu.user + cpu.system))
        except psutil.Error:
          return
    worker = threading.Thread(target=monitor)
    worker.start()
    try:
      raw, error = process.communicate(frame * 21, timeout=30)
      if process.returncode:
        raise RuntimeError(error.decode(errors='replace'))
      elapsed = time.perf_counter() - started
    finally:
      if process.poll() is None:
        process.kill()
        process.wait()
      stop.set()
      worker.join(1)
    results.append({'threads': threads, 'frames': 20, 'elapsed_s': elapsed,
                    'amortized_ms_per_frame': elapsed * 1000 / 20, 'encoded_bytes': len(raw),
                    'sampled_peak_rss': max((v[0] for v in samples), default=0),
                    'sampled_cpu_s': max((v[1] for v in samples), default=0)})
  return results


def soak(seconds):
  sys.path.insert(0, str(Path(os.environ['CARROT_JETSON']) / 'third_party/jetlink'))
  from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
  from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport
  from openpilot.selfdrive.modeld.jetlink.protocol import Msg
  from openpilot.tools.jetson import ncm_display as display
  from openpilot.tools.jetson.ncm_server import transport_class
  from openpilot.tools.jetson.hud_tx import HudTx
  from jetlink.transport.base import LinkTimeout
  from openpilot.tools.jetson.hud_observations import observed_objects
  from openpilot.tools.jetson.hud_signals import RemoteSignals
  from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet
  template = {'version': 1, 'session': 'a' * 32, 'sent': 1., 'events': {},
    'params': {}, 'mono': {}, 'received': {}, 'valid': {}, 'alive': {}, 'cameras': {}}
  proc = psutil.Process()
  rss, latencies = [], []
  started = time.monotonic()
  cpu_start = time.process_time()
  connections = frames = 0
  with tempfile.TemporaryDirectory(prefix='nexo-local-soak-') as directory:
    display.SNAPSHOT = Path(directory) / 'hud.json'
    display.RUNTIME = Path(directory)
    while time.monotonic() - started < seconds:
      listener = socket.socket()
      listener.bind(('127.0.0.1', 0))
      listener.listen(1)
      sock = socket.create_connection(listener.getsockname())
      peer, _ = listener.accept()
      listener.close()
      client = JetlinkClient(JetlinkNcmTcpTransport(sock), name='nexo-ncm-display')
      server = display.DisplaySession(transport_class()(peer), SimpleNamespace(send=lambda _: None, close=lambda: None))
      errors = []
      def serve(server=server, errors=errors):
        try:
          received = 0
          while received < 21:
            server.check_alive()
            try:
              server.handle(server.t.recv(.1))
              received += 1
            except LinkTimeout:
              continue  # Production receive_session also retries a partial poll.
        except Exception as error:
          errors.append(str(error))
      worker = threading.Thread(target=serve)
      worker.start()
      try:
        hello = client.hello()
        writer = HudTx(client.t, host_clock_floor=hello['jetson_hello_mono'] - time.monotonic())
        receipt = display.HudReceipt(writer, True)
        reader = RemoteSignals()
        for index in range(10):
          before = time.monotonic()
          color = 'green' if index % 2 else 'red'
          template.update(sent=before, cameras={'road': {'frame': index, 'time': before,
            'source_width': 1344, 'source_height': 760, 'jpeg': 'fixture'}},
            signal_display={'version': 2, 'record': {'version': 1, 'stream': 'road', 'size': [1344, 760],
              'timestamp': before, 'frame_id': index, 'session': 'soak',
              'tracks': [{'id': 1, 'age': 0, 'box': [10, 10, 30, 30], 'raw': color}]}})
          packet = snapshot_packet(json.dumps(template).encode(), 'a' * 32)
          receipt.send(Msg.HUD, client._next_seq(), [packet])
          while not writer.idle:
            writer.pump()
            if not writer.idle:
              time.sleep(.005)
          assert writer.last_hud_seq == receipt.last_seq
          assert receipt.check(client.state())
          assert server.last_hud_seq == receipt.last_seq
          latencies.append((time.monotonic() - before) * 1000)
          assert observed_objects({'width': 100, 'height': 100, 'objects': [
            {'name': 'car', 'conf': .9, 'x1': 1, 'x2': 5, 'y1': 2, 'y2': 7}]})
          received = json.loads(display.SNAPSHOT.read_text())
          lights = reader.read(received, time.monotonic())
          assert lights and lights[0]['kind'] == color, (received, vars(reader), time.monotonic())
          frames += 1
          time.sleep(.1)  # Match the unchanged production HUD cadence.
        worker.join(3)
        assert not worker.is_alive() and not errors, errors
      except Exception as error:
        raise RuntimeError(f'soak failed after {connections} connections/{frames} frames; server_errors={errors}; cause={error}') from error
      finally:
        client.close()
        server.close()
        worker.join(3)
      assert not display.SNAPSHOT.exists()
      connections += 1
      rss.append(proc.memory_info().rss)
  return {'elapsed_s': time.monotonic() - started, 'connections': connections, 'frames': frames,
          'cpu_s': time.process_time() - cpu_start, 'tcp_ack_ms_median': statistics.median(latencies),
          'tcp_ack_ms_max': max(latencies), 'rss_first': rss[0], 'rss_last': rss[-1], 'rss_max': max(rss),
          'remaining_threads': threading.active_count(), 'errors': 0, 'physical_usb_verified': False,
          'writer': 'display_only_cooperative_latest_snapshot'}


def signal_benchmark():
  from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet
  from openpilot.tools.jetson.hud_signals import RemoteSignals
  results = []
  for count in (0, 20):
    value = {'version': 1, 'sent': 100.1, 'updated': 10., 'events': {}, 'params': {}, 'mono': {},
             'received': {}, 'valid': {}, 'alive': {}, 'cameras': {'road': {'frame': 1, 'time': 100.,
               'source_width': 1344, 'source_height': 760, 'jpeg': 'fixture'}}}
    if count:
      value['signal_display'] = {'version': 2, 'record': {'version': 1, 'stream': 'road', 'size': [1344, 760],
        'timestamp': 100., 'frame_id': 1, 'session': 'bench',
        'tracks': [{'id': i + 1, 'age': 0, 'box': [10, 10, 30, 30], 'raw': 'green'} for i in range(count)]}}
    raw, samples, reader = json.dumps(value).encode(), [], RemoteSignals()
    for _ in range(1000):
      started = time.perf_counter_ns()
      packed = snapshot_packet(raw, 'a' * 32)
      lights = reader.read(json.loads(packed), 10.)
      samples.append((time.perf_counter_ns() - started) / 1e6)
      assert len(lights) == count
    results.append({'tracks': count, 'iterations': len(samples), 'wire_bytes': len(packed),
                    'processing_ms_median': statistics.median(samples),
                    'processing_ms_p95': sorted(samples)[int(len(samples) * .95)], 'processing_ms_max': max(samples)})
  return results


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--seconds', type=int, default=60, choices=range(1, 3601))
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  result = {'environment': sys.version, 'scope': 'localhost TCP and desktop software FFmpeg only',
            'signal_processing': signal_benchmark(), 'encoder': encoder_benchmark(), 'soak': soak(args.seconds)}
  args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
  print(json.dumps(result, indent=2))


if __name__ == '__main__':
  main()
