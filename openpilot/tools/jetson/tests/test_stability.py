"""No hardware I/O: actual lifecycle/recovery policy on synthetic procfs/sockets."""
import gc
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import weakref

import pytest

from openpilot.common.stability import MemoryObserver, usb_power_observation
from openpilot.tools.jetson import recovery
from openpilot.tools.jetson.state import public_telemetry
from openpilot.tools.jetson.stream import EncodedInput


def memory(root, available=500):
  (root / 'meminfo').write_text(f'MemTotal: 1000 kB\nMemAvailable: {available} kB\n')


def process(root, pid, start, rss=10):
  entry = root / str(pid)
  entry.mkdir(exist_ok=True)
  (entry / 'cmdline').write_bytes(b'python\0orin_yolo_direct.py\0')
  fields = ['0'] * 50
  fields[19], fields[21] = str(start), str(rss)
  (entry / 'stat').write_text(f'{pid} (worker with spaces) ' + ' '.join(fields))


def test_memory_sampling_is_throttled_bounded_and_pid_reuse_safe(tmp_path):
  memory(tmp_path)
  process(tmp_path, 1, 10)
  observer = MemoryObserver(tmp_path)
  first = observer.sample(0)
  assert first['used_bytes'] == 500 * 1024
  memory(tmp_path, 400)
  assert observer.sample(4) is first
  process(tmp_path, 1, 10, 20)
  second = observer.sample(5)
  assert second['growth_bytes'] == 100 * 1024
  assert second['processes'][0]['growth_bytes'] > 0
  process(tmp_path, 1, 11, 30)
  assert observer.sample(10)['processes'][0]['growth_bytes'] is None
  for index in range(1, 501):
    observer.sample(10 + index * 5)
  assert len(observer.samples) == 61
  assert observer.cached['window_s'] == 300
  (tmp_path / 'meminfo').unlink()
  assert observer.sample(3000) == {'available': False}
  assert not observer.samples  # Never present old samples as current readings.


def test_missing_exited_and_excess_processes_are_bounded(tmp_path):
  memory(tmp_path)
  for pid in range(50):
    process(tmp_path, pid, pid)
  (tmp_path / '0/stat').unlink()
  result = MemoryObserver(tmp_path).sample(0)
  assert len(result['processes']) == 32
  assert all(p['pid'] != 0 for p in result['processes'])


def test_usb_power_observation_never_writes_or_claims_compatibility(tmp_path):
  files = {'sys/class/usbpd/usbpd0/current_pr': 'sink\n',
           'sys/kernel/debug/pmic-votable/USB_ICL/force_active': '0\n',
           'sys/kernel/debug/pmic-votable/USB_ICL/force_val': '900000\n'}
  for name, text in files.items():
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
  result = usb_power_observation(tmp_path)
  assert result['mode'] == 'diagnostic_only' and result['hardware_verified'] is False
  assert result['data_role'] is None
  assert {name: (tmp_path / name).read_text() for name in files} == files


def test_memory_telemetry_is_bounded_and_contains_no_commands_or_credentials():
  unsafe = {'available': True, 'used_bytes': 100, 'password': 'private',
            'processes': [{'pid': 1, 'role': 'yolo', 'rss_bytes': 20, 'cmdline': 'secret'}] * 100}
  value = public_telemetry({'telemetry': {'nexo_memory': unsafe}, 'nexo_hud_recovery': {'last_error': 'token=private'}})
  assert len(value['telemetry']['nexo_memory']['processes']) == 32
  assert 'private' not in json.dumps(value) and 'cmdline' not in json.dumps(value)


def test_stream_renews_one_subscription_resolves_new_address_and_releases_previous(monkeypatch):
  from openpilot.tools.jetson import stream
  now = [0.]
  sockets, addresses = [], []
  ip = ['192.0.2.1']
  monkeypatch.setattr(stream.socket, 'gethostbyname', lambda host: ip[0])
  def subscribe(*args, **kwargs):
    addresses.append(kwargs['addr'])
    sock = SimpleNamespace(closed=False)
    sock.close = lambda: setattr(sock, 'closed', True)
    sockets.append(sock)
    assert kwargs['timeout'] == 1000 and kwargs['conflate'] is False
    assert sum(not s.closed for s in sockets) == 1
    return sock
  events = [None]
  messages = SimpleNamespace(sub_sock=subscribe, recv_one=lambda sock: events[0])
  owner = EncodedInput(messages, 'configured-comma', clock=lambda: now[0])
  now[0] = 4.99
  assert owner.receive() is None and owner.reconnects == 0
  ip[0] = '192.0.2.2'
  now[0] = 5.
  assert owner.receive() is None and owner.reconnects == 1
  assert addresses == ['192.0.2.1', '192.0.2.2'] and sockets[0].closed
  events[0] = object()
  assert owner.receive() is events[0]  # Does not drain/drop dependent HEVC packets.
  owner.close()
  assert all(s.closed for s in sockets)


def test_hud_failure_reclaims_cycles_before_wait_and_backs_off(monkeypatch, tmp_path):
  monkeypatch.setattr(recovery, 'RUNTIME', tmp_path)
  refs, delays = [], []
  class Cycle:
    pass
  class Done(BaseException):
    pass
  def run():
    value = Cycle()
    value.self = value
    refs.append(weakref.ref(value))
    raise ConnectionError('USB panel unplugged')
  def pause(seconds):
    assert all(ref() is None for ref in refs)
    assert not (tmp_path / 'hud-status.json').exists()
    delays.append(seconds)
    if len(delays) == 4:
      raise Done
  with pytest.raises(Done):
    recovery.run_hud_sessions(run, lambda: True, gc.collect, clock=lambda: 0., sleep=pause)
  assert delays == [2., 4., 60., 60.]
  status = json.loads((tmp_path / 'hud-recovery.json').read_text())
  assert status['attempts'] == 4 and 'USB panel unplugged' in status['last_error']


def test_hud_waits_without_allocating_renderer(monkeypatch, tmp_path):
  monkeypatch.setattr(recovery, 'RUNTIME', tmp_path)
  class Done(BaseException):
    pass
  def pause(_):
    raise Done
  with pytest.raises(Done):
    recovery.run_hud_sessions(lambda: pytest.fail('renderer started without a panel/snapshot'),
                              lambda: False, gc.collect, sleep=pause)


def test_yolo_installers_bound_crash_restarts():
  root = Path(__file__).resolve().parents[2] / 'camerastream'
  for name in ('install_nexo_yolo_direct_service.sh', 'install_nexo_yolo_service.sh'):
    source = (root / name).read_text()
    assert 'StartLimitBurst=3' in source and 'StartLimitIntervalSec=300' in source
    assert 'RestartSec=10' in source and 'KillMode=control-group' in source


@pytest.mark.skipif(sys.platform != 'linux', reason='requires real Linux flock')
def test_second_yolo_owner_is_rejected_until_first_session_exits(tmp_path):
  from openpilot.tools.jetson.stream import single_yolo_instance
  path = tmp_path / 'yolo.lock'
  with single_yolo_instance(path):
    with pytest.raises(BlockingIOError), single_yolo_instance(path):
      pytest.fail('two inference processes acquired the same lock')
  with single_yolo_instance(path):
    pass


def test_7000_stability_expires_remote_memory_and_reports_power_read_only(monkeypatch, tmp_path):
  from openpilot.selfdrive.carrot.server.services import jetson
  monkeypatch.setattr(jetson, 'DISPLAY_STATUS', tmp_path / 'missing')
  monkeypatch.setattr(jetson, 'LINK_STATUS', ())
  monkeypatch.setattr(jetson, 'MODEL_STATUS', ())
  monkeypatch.setattr(jetson, 'UDC_ROOT', tmp_path)
  packet = {'nexo_memory': {'available': True, 'used_bytes': 1000, 'processes': []},
            'yolo_recent': False, 'yolo_service': {'NRestarts': '3', 'Result': 'start-limit-hit', 'password': 'private'}}
  state = {'channels': {'udp': {'received_mono': 10., 'source_ip': '192.0.2.1', 'packet': packet}}}
  result = jetson.snapshot(state, 11.)
  assert result['stability']['jetson_memory']['used_bytes'] == 1000
  assert result['stability']['yolo_service'] == {'NRestarts': '3', 'Result': 'start-limit-hit'}
  assert result['stability']['usb_power']['mode'] == 'diagnostic_only'
  expired = jetson.snapshot(state, 16.)
  assert not expired['connected'] and not expired['stability']['jetson_memory']


def test_broken_wifi_socket_retries_only_after_interval(monkeypatch, tmp_path):
  from openpilot.tools.jetson import host
  monkeypatch.setattr(host, 'RUNTIME', tmp_path)
  now = [10.]
  monkeypatch.setattr(host.time, 'monotonic', lambda: now[0])
  attempts = []
  def unavailable(*args, **kwargs):
    attempts.append(now[0])
    raise OSError('no network')
  source = host.VideoSource(SimpleNamespace(pub_sock=lambda *args: None, sub_sock=unavailable), '192.0.2.1', True)
  source.select(False)
  for _ in range(100):
    source.poll_wifi()
  assert attempts == [10.]
  now[0] = 15.
  source.poll_wifi()
  assert attempts == [10., 15.] and source.reconnects == 1
