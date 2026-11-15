"""USB-bound diagnostic traffic only; sockets/SSH explicitly simulated."""
import json
import socket
import struct
from types import SimpleNamespace as NS

import pytest

from openpilot.tools.jetson import lte_probe as probe
from openpilot.tools.jetson import lte_share as gateway


def reply(ident=17, *, flags=0x8180, address=b'\x8c\x52\x70\x04'):
  return (struct.pack('!6H', ident, flags, 1, 1, 0, 0) + b'\x06github\x03com\x00\x00\x01\x00\x01'
          + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + address)


def test_fixed_dns_question_and_answer_are_validated():
  assert probe.dns_addresses(reply(), 17) == ['140.82.112.4']


@pytest.mark.parametrize('data', [b'', reply(18), reply(flags=0x8380), reply(flags=0x8183),
  reply()[:-1], reply().replace(b'github', b'gitlab'), reply()[:12] + b'\xff'])
def test_malformed_mismatched_failed_or_truncated_dns_never_qualifies(data):
  with pytest.raises((ValueError, struct.error)):
    probe.dns_addresses(data, 17)


@pytest.mark.parametrize('usb_success', [False, True])
def test_wifi_system_dns_cannot_qualify_usb_internet(monkeypatch, usb_success):
  calls = []
  class Sock:
    def __enter__(self):
      return self
    def __exit__(self, *args):
      self.close()
    def settimeout(self, value):
      assert value == 3
    def setsockopt(self, level, option, iface):
      assert (level, option, iface) == (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'enxusb\x00')
      calls.append('usb-bound')
    def bind(self, address):
      assert address == (probe.SOURCE, 0)
    def connect(self, address):
      if not usb_success:
        raise OSError('USB path unavailable; Wi-Fi still works')
    def send(self, packet):
      self.ident = struct.unpack_from('!H', packet)[0]
    def sendall(self, packet):
      assert b'Host: github.com' in packet
    def recv(self, size):
      return reply(self.ident) if size == 4096 else b'HTTP/1.1 200 OK\r\n'
    def close(self):
      pass
  monkeypatch.setattr(probe.socket, 'socket', lambda *a: Sock())
  def command(args, **kw):
    if args[0] == 'ip':
      assert args[-2:] == ['oif', 'enxusb']
      return NS(stdout=json.dumps([{'dev': 'enxusb', 'gateway': probe.GATEWAY}]))
    assert args == ['getent', 'ahostsv4', 'github.com']
    return NS(returncode=0, stdout='140.82.112.4')
  monkeypatch.setattr(probe.subprocess, 'run', command)
  def tls(sock, server_hostname):
    assert server_hostname == 'github.com'
    return sock
  monkeypatch.setattr(probe.ssl, 'create_default_context', lambda: NS(wrap_socket=tls))
  value = probe.probe('enxusb')
  assert value['system_dns_success'] is True
  assert value['internet_dns_verified'] is usb_success
  assert value['network_configuration_changed'] is False
  assert len(calls) == (2 if usb_success else 1)


def test_connectivity_probe_requires_offroad_before_ssh(monkeypatch):
  monkeypatch.setattr(gateway, 'offroad', lambda: False)
  monkeypatch.setattr(gateway, 'command', lambda *a, **kw: pytest.fail('Onroad probe attempted'))
  with pytest.raises(gateway.GatewayError, match='Offroad'):
    gateway.connectivity_probe()


def test_successful_probe_uses_fixed_ssh_script_and_expires(monkeypatch, tmp_path):
  state = {'usb': 'usb0', 'wan': 'ppp0', 'host': 'enxusb'}
  monkeypatch.setattr(gateway, 'offroad', lambda: True)
  monkeypatch.setattr(gateway, 'load', lambda: state)
  monkeypatch.setattr(gateway, 'status', lambda: {'enabled': True, 'wan_route_matches_config': True})
  monkeypatch.setattr(gateway, 'jetson_ncm', lambda: 'enxusb')
  monkeypatch.setattr(gateway, 'PROBE', tmp_path / 'probe.json')
  boot = tmp_path / 'boot'
  boot.write_text('one-boot')
  monkeypatch.setattr(gateway, 'BOOT', boot)
  now = [100.]
  monkeypatch.setattr(gateway.time, 'monotonic', lambda: now[0])
  def command(args, **kwargs):
    assert args[-1] == 'sudo -n python3 - enxusb'
    assert 'SO_BINDTODEVICE' in kwargs['stdin_text']
    return NS(stdout=json.dumps({'network_configuration_changed': False, 'source': gateway.JETSON,
      'interface': 'enxusb', 'usb_dns_success': True, 'usb_https_success': True}))
  monkeypatch.setattr(gateway, 'command', command)
  assert gateway.connectivity_probe()['internet_dns_verified'] is True
  assert gateway.probe_record(state)['internet_dns_verified'] is True
  now[0] += 121
  assert gateway.probe_record(state)['internet_dns_verified'] is False
  now[0] = 100.
  boot.write_text('next-boot')
  assert gateway.probe_record(state)['internet_dns_verified'] is False
