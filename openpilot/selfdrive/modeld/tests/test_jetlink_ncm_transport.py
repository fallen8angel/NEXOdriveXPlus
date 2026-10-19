"""Regression coverage for experimental Jetlink over existing AGNOS g1 USB-NCM."""
import socket
import subprocess

import pytest

from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm
from openpilot.selfdrive.modeld.jetlink import protocol as P
from openpilot.tools.jetson.transport.base import LinkError


def test_transport_mode_is_off_by_default(tmp_path, monkeypatch):
  mode = tmp_path / 'transport'
  monkeypatch.setattr(ncm, 'MODE_FILE', mode)
  assert ncm.transport_mode() == 'usb'
  mode.write_text('ncm-tcp\n')
  assert ncm.transport_mode() == 'ncm-tcp'
  mode.write_text('tcp://0.0.0.0:5599')
  with pytest.raises(ValueError, match='unsupported'):
    ncm.transport_mode()


def test_ncm_rejects_wifi_route_before_connect(monkeypatch):
  def run(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, '192.168.100.250 via 172.20.10.1 dev wlan0 src 172.20.10.11', '')
  monkeypatch.setattr(ncm.subprocess, 'run', run)
  with pytest.raises(LinkError, match='not via usb0'):
    ncm.require_usb_route()


def test_ncm_rejects_unreachable_peer(monkeypatch):
  monkeypatch.setattr(ncm.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 2, '', 'network unreachable'))
  with pytest.raises(LinkError, match='not via usb0'):
    ncm.require_usb_route()


def test_ncm_routes_usb0_only(monkeypatch):
  # 'lo' provides an actual sysfs interface without needing real USB hardware.
  monkeypatch.setattr(ncm, 'USB_IFACE', 'lo')
  monkeypatch.setattr(ncm.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(
    a, 0, '192.168.100.250 dev lo src 192.168.100.127', ''))
  assert 'dev lo' in ncm.require_usb_route()


def test_wire_v2_round_trip_over_stream():
  # Socket pair checks stream framing; does NOT validate Jetson/HUD hardware.
  left, right = socket.socketpair()
  a = ncm.JetlinkNcmTcpTransport(left)
  b = ncm.JetlinkNcmTcpTransport(right)
  try:
    a.send(P.Msg.PING, 23, (b'hello',))
    msg = b.recv(timeout=0.5)
    assert msg.msg_type == P.Msg.PING
    assert msg.seq == 23
    assert bytes(msg.payload) == b'hello'
    b.send(P.Msg.PONG, 24, (b'ok',))
    msg = a.recv(timeout=0.5)
    assert msg.msg_type == P.Msg.PONG
    assert bytes(msg.payload) == b'ok'
  finally:
    a.close()
    b.close()
