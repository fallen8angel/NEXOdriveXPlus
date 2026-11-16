"""CI-only real packet test in a fresh root network AND mount namespace.

Explicitly invoked by hud-recovery.yaml, never by ordinary test discovery.
Hardware identity/Offroad reads are simulated; routes, NAT and forwarding run
against the real kernel. No vehicle, physical USB, LTE modem or Internet.
"""
import os
import subprocess
import uuid

import pytest

from openpilot.tools.jetson import lte_uplink

from openpilot.tools.jetson import lte_share as gateway


@pytest.mark.parametrize('policy_mode', [False, True], ids=['cellular-default', 'wifi-default-existing-lte-policy'])
def test_real_packets_require_scoped_nat_and_cleanup_keeps_foreign_rules(monkeypatch, tmp_path, policy_mode):
  assert os.geteuid() == 0, 'Must be explicitly run as root inside unshare --mount --net'
  assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net'), 'Refusing the host network namespace'
  assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt'), 'Refusing the host mount namespace'
  suffix = uuid.uuid4().hex[:8]
  jetson, wan = 'nexo-j-' + suffix, 'nexo-w-' + suffix
  created = []
  interfaces = []
  wan_if = 'ppp0' if policy_mode else 'rmnet_data0'
  def run(*args, check=True):
    result = subprocess.run(list(args), check=False, capture_output=True, text=True, timeout=8)
    if check:
      assert result.returncode == 0, f'{args}: {result.stderr} {result.stdout}'
    return result
  original = gateway.command
  def local(args, **kwargs):
    # Already root in the isolated namespace; no need for a sudo helper.
    return original(args[2:] if args[:2] == ['sudo', '-n'] else args, **kwargs)
  def remote(*args, optional=False):
    args = args[2:] if args[:2] == ('sudo', '-n') else args
    return original(['ip', 'netns', 'exec', jetson, *args], optional=optional)
  monkeypatch.setattr(gateway, 'command', local)
  monkeypatch.setattr(gateway, 'remote', remote)
  monkeypatch.setattr(gateway, 'offroad', lambda: True)  # No vehicle exists in this kernel namespace.
  monkeypatch.setattr(gateway, 'find_comma_ncm', lambda: 'usb0')
  monkeypatch.setattr(gateway, 'jetson_ncm', lambda: 'enxusb')
  monkeypatch.setattr(gateway, 'STATE', tmp_path / 'state.json')
  monkeypatch.setattr(gateway, 'PROBE', tmp_path / 'probe.json')
  try:
    for name in (jetson, wan):
      run('ip', 'netns', 'add', name)
      created.append(name)
      run('ip', '-n', name, 'link', 'set', 'lo', 'up')
    run('ip', 'link', 'set', 'lo', 'up')
    for local_if, peer_if, namespace, local_ip, peer_ip in (
        ('usb0', 'enxusb', jetson, gateway.COMMA, gateway.JETSON),
        (wan_if, 'wanpeer', wan, '10.73.0.1', '10.73.0.2')):
      run('ip', 'link', 'add', local_if, 'type', 'veth', 'peer', 'name', peer_if)
      interfaces.append(local_if)
      run('ip', 'link', 'set', peer_if, 'netns', namespace)
      run('ip', 'addr', 'add', local_ip + '/24', 'dev', local_if)
      run('ip', 'link', 'set', local_if, 'up')
      run('ip', '-n', namespace, 'addr', 'add', peer_ip + '/24', 'dev', peer_if)
      run('ip', '-n', namespace, 'link', 'set', peer_if, 'up')
    if policy_mode:
      # Simulated hardware identity only; policy lookup/NAT/packets are real.
      run('ip', 'link', 'add', 'wlan0', 'type', 'dummy')
      interfaces.append('wlan0')
      run('ip', 'addr', 'add', '192.0.2.2/24', 'dev', 'wlan0')
      run('ip', 'link', 'set', 'wlan0', 'up')
      run('ip', 'route', 'add', 'default', 'via', '192.0.2.1', 'dev', 'wlan0')
      run('ip', 'route', 'add', 'default', 'via', '10.73.0.2', 'dev', wan_if, 'table', '1000')
      monkeypatch.setattr(gateway, 'sharing_uplink', lambda: {
        'eligible': True, 'wan': wan_if, 'route_mode': 'cellular_policy'})
    else:
      run('ip', 'route', 'add', 'default', 'via', '10.73.0.2', 'dev', wan_if)
    main_routes = run('ip', '-j', 'route', 'show', 'table', 'main').stdout
    run('sysctl', '-w', 'net.ipv4.ip_forward=0')
    backend = gateway.select_firewall_backend()
    run(backend, '-P', 'FORWARD', 'DROP')  # Safe ONLY after the fresh namespace assertions above.
    foreign = ['-s', '203.0.113.9/32', '-m', 'comment', '--comment', 'unrelated-ci-rule', '-j', 'DROP']
    run(backend, '-A', 'FORWARD', *foreign)
    def ping():
      return run('ip', 'netns', 'exec', jetson, 'ping', '-n', '-c', '1', '-W', '1', '10.73.0.2', check=False)
    assert ping().returncode != 0  # No default route, no forwarding.
    assert 'enabled' in gateway.enable()
    assert gateway.status()['enabled'] is True
    assert ping().returncode == 0  # WAN has NO route back to 192.168.100.0/24: this requires NAT.
    state = gateway.load()
    nat_chain, nat_rule = gateway.rules(state['usb'], state['wan'])[0]
    gateway.iptables(nat_chain, nat_rule, '-D')
    # A new destination avoids reusing the existing conntrack translation.
    run('ip', '-n', wan, 'addr', 'add', '10.73.0.3/24', 'dev', 'wanpeer')
    assert run('ip', 'netns', 'exec', jetson, 'ping', '-n', '-c', '1', '-W', '1',
               '10.73.0.3', check=False).returncode != 0
    gateway.iptables(nat_chain, nat_rule, '-I')
    if policy_mode:
      assert lte_uplink.verified(local, 'usb0')
      run('ip', 'netns', 'exec', jetson, 'ping', '-n', '-c', '1', '-W', '1', gateway.COMMA)
      run('ip', 'route', 'del', 'default', 'table', '1000')
      run('ip', '-n', wan, 'addr', 'add', '10.73.0.4/24', 'dev', 'wanpeer')
      # Main has a connected WAN route: the guard MUST prevent fallback.
      assert run('ip', 'netns', 'exec', jetson, 'ping', '-n', '-c', '1', '-W', '1',
                 '10.73.0.4', check=False).returncode != 0
      assert gateway.status()['enabled'] is False
    assert 'disabled' in gateway.disable()
    assert run('ip', '-j', 'route', 'show', 'table', 'main').stdout == main_routes
    assert not any(r.get('priority') in lte_uplink.PRIORITIES for r in lte_uplink.rule_rows(local))
    assert gateway.IP_FORWARD.read_text().strip() == '0'
    assert not gateway.STATE.exists()
    assert not gateway.jetson_routes()
    run(backend, '-C', 'FORWARD', *foreign)
    assert gateway.COMMENT not in run(backend, '-t', 'nat', '-S', 'POSTROUTING').stdout
    assert gateway.COMMENT not in run(backend, '-S', 'FORWARD').stdout
  finally:
    # netns deletion may defer veth destruction while kernel references drain.
    # Explicitly remove only the interfaces this test created before the next case.
    for iface in reversed(interfaces):
      run('ip', 'link', 'del', iface, check=False)
    for name in reversed(created):
      run('ip', 'netns', 'del', name, check=False)
