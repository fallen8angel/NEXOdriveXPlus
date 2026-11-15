"""Explicit bounded USB Internet probe, streamed to Jetson's existing SSH.

Only socket traffic and route/resolver reads. No DNS, route, firewall or service
configuration. A Wi-Fi success cannot qualify as a USB/LTE success.
"""
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys

SOURCE = '192.168.100.250'
GATEWAY = '192.168.100.127'
HOST = 'github.com'


def dns_addresses(packet, query_id):
  if len(packet) < 12:
    raise ValueError('Short DNS response')
  ident, flags, questions, answers, _, _ = struct.unpack_from('!6H', packet)
  if ident != query_id or flags & 0x820F != 0x8000 or questions != 1 or answers > 64:
    raise ValueError('Invalid/truncated/failed DNS reply')
  def skip_name(offset):
    for _ in range(128):
      if offset >= len(packet):
        raise ValueError('Invalid DNS name')
      size = packet[offset]
      if size & 0xC0 == 0xC0:
        if offset + 1 >= len(packet):
          raise ValueError('Short DNS pointer')
        return offset + 2
      if size > 63:
        raise ValueError('Invalid DNS label')
      offset += size + 1
      if size == 0:
        return offset
    raise ValueError('DNS name too long')
  offset = skip_name(12)
  if packet[12:offset] != b'\x06github\x03com\x00' or packet[offset:offset + 4] != b'\x00\x01\x00\x01':
    raise ValueError('DNS question differs from the fixed probe target')
  offset += 4
  addresses = []
  for _ in range(answers):
    offset = skip_name(offset)
    kind, cls, _, size = struct.unpack_from('!HHIH', packet, offset)
    offset += 10
    if offset + size > len(packet):
      raise ValueError('Truncated DNS answer')
    if kind == 1 and cls == 1 and size == 4:
      addresses.append(socket.inet_ntoa(packet[offset:offset + size]))
    offset += size
  if not addresses:
    raise ValueError('No IPv4 answer')
  return addresses


def bound_socket(iface, kind):
  sock = socket.socket(socket.AF_INET, kind)
  try:
    sock.settimeout(3)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b'\x00')
    sock.bind((SOURCE, 0))
    return sock
  except BaseException:
    sock.close()
    raise


def probe(iface):
  if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', iface):
    raise ValueError('Invalid USB interface')
  result = {'network_configuration_changed': False, 'usb_dns_success': False,
            'usb_https_success': False, 'system_dns_success': False, 'internet_dns_verified': False,
            'source': SOURCE, 'interface': iface, 'dns_server': '1.1.1.1', 'target': HOST, 'failures': []}
  try:
    response = subprocess.run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1', 'from', SOURCE, 'oif', iface],
                              check=True, capture_output=True, text=True, timeout=2)
    route = json.loads(response.stdout)
    result['usb_route'] = route
    if len(route) != 1 or route[0].get('dev') != iface or route[0].get('gateway') != GATEWAY:
      raise ValueError('USB-bound Internet route does not use the Comma gateway')
    ident = secrets.randbits(16)
    query = struct.pack('!6H', ident, 0x0100, 1, 0, 0, 0) + b'\x06github\x03com\x00\x00\x01\x00\x01'
    with bound_socket(iface, socket.SOCK_DGRAM) as sock:
      sock.connect(('1.1.1.1', 53))
      sock.send(query)
      addresses = dns_addresses(sock.recv(4096), ident)
    result.update(usb_dns_success=True, usb_dns_addresses=addresses)
    context = ssl.create_default_context()
    with bound_socket(iface, socket.SOCK_STREAM) as raw:
      raw.connect((addresses[0], 443))
      with context.wrap_socket(raw, server_hostname=HOST) as tls:
        tls.sendall(b'HEAD / HTTP/1.1\r\nHost: github.com\r\nConnection: close\r\n\r\n')
        line = tls.recv(512).split(b'\r\n', 1)[0].decode('ascii', errors='replace')
        if not re.match(r'HTTP/1\.[01] [23][0-9]{2}(?: |$)', line):
          raise ValueError('HTTPS response did not succeed: ' + line[:100])
        result.update(usb_https_success=True, https_status=line[:100])
  except (OSError, ValueError, struct.error, subprocess.SubprocessError) as error:
    result['failures'].append({'code': 'USB_DNS_OR_HTTPS_FAILED', 'detail': str(error)[:240]})
  try:
    # Separate system resolver result; this path may use Wi-Fi. Do not use it
    # to qualify the USB/LTE test and never rewrite /etc/resolv.conf.
    resolved = subprocess.run(['getent', 'ahostsv4', HOST], capture_output=True, text=True, timeout=3)
    result['system_dns_success'] = resolved.returncode == 0 and bool(resolved.stdout.strip())
    if not result['system_dns_success']:
      result['failures'].append({'code': 'SYSTEM_DNS_FAILED', 'detail': 'System resolver returned no IPv4 answer'})
  except (OSError, subprocess.SubprocessError) as error:
    result['failures'].append({'code': 'SYSTEM_DNS_FAILED', 'detail': str(error)[:160]})
  try:
    with Path('/etc/resolv.conf').open() as source:
      result['system_nameservers'] = [line.strip() for line in source.read(4096).splitlines()
                                      if line.lstrip().startswith('nameserver')][:4]
  except OSError:
    result['system_nameservers'] = []
  result['internet_dns_verified'] = result['usb_dns_success'] and result['usb_https_success']
  return result


if __name__ == '__main__':
  if len(sys.argv) != 2:
    raise SystemExit('One verified USB interface required')
  print(json.dumps(probe(sys.argv[1]), ensure_ascii=False))
