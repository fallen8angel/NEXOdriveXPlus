"""Read-only HUD soak regression tests: no vehicle, panel or network required."""
import json

from openpilot.tools.jetson import hud_link_soak as monitor


def put_json(directory, name, record):
  (directory / name).write_text(json.dumps(record), encoding='utf-8')


def usb_node(directory, name, vid, pid):
  node = directory / name
  node.mkdir()
  (node / 'idVendor').write_text(vid)
  (node / 'idProduct').write_text(pid)


def iface_node(directory, carrier):
  node = directory / 'usb0'
  (node / 'statistics').mkdir(parents=True)
  (node / 'carrier').write_text(str(carrier))
  (node / 'operstate').write_text('up' if carrier else 'down')
  (node / 'statistics/rx_bytes').write_text('100')
  (node / 'statistics/tx_bytes').write_text('200')


def test_usb_panel_and_ncm_are_independent(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '1-1', '1cbe', '0092')
  iface_node(net, 0)
  assert monitor.usb_panel(usb)['status'] == 'present'
  assert monitor.ncm_interface('usb0', net)['carrier'] is False
  assert monitor.ncm_interface('enx-missing', net)['status'] == 'not_found'


def test_missing_sysfs_is_unknown_not_cable_failure(tmp_path):
  assert monitor.usb_panel(tmp_path / 'missing')['status'] == 'unknown'
  assert monitor.ncm_interface(None)['status'] == 'not_selected'
  assert monitor.bounded_json(tmp_path / 'missing.json') == {}


def test_historical_transport_error_does_not_override_current_health(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '1-2', '1cbe', '0123')
  iface_node(net, 1)
  put_json(tmp_path, 'ncm-display.json', {
    'updated': 100., 'state': 'connected', 'hello_complete': True,
    'last_hud_tx_mono': 99.9, 'last_peer_rx_mono': 99.8,
    'connection_anomalies': 3, 'transport_recoveries': 4,
    'last_transport_error': {'detail': 'historical broken pipe'}})
  put_json(tmp_path, 'hud-status.json', {
    'updated': 100., 'last_panel_send_mono': 99.9, 'display_mode': 'vehicle',
    'encoded_expired_frames': 2})
  result = monitor.sample('jetson', 'usb0', tmp_path, usb, net, now=100.1)
  assert result['tcp']['historical_error_recorded']
  assert result['assessment'] == 'panel_write_recent_not_physical_proof'
  assert result['physical_hud_verified'] is False


def test_current_ncm_failure_takes_precedence_over_connected_panel(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '1-1', '1cbe', '0092')
  iface_node(net, 0)
  put_json(tmp_path, 'ncm-display.json', {'updated': 100., 'state': 'retrying',
    'hello_complete': False, 'last_transport_error': {'detail': 'timeout'}})
  result = monitor.sample('jetson', 'usb0', tmp_path, usb, net, now=100.1)
  assert result['assessment'] == 'ncm_usb_link_loss_possible'
  assert result['panel_usb']['status'] == 'present'


def test_stale_tcp_is_not_current_link_failure(tmp_path):
  put_json(tmp_path, 'ncm-display.json', {'updated': 10., 'state': 'retrying',
    'last_transport_error': {'detail': 'disconnected'}})
  result = monitor.sample('comma', runtime=tmp_path, now=100.)
  assert result['tcp']['status'] == 'stale'
  assert result['tcp']['state'] is None
  assert result['assessment'] == 'insufficient_evidence'


def test_panel_absent_when_renderer_running(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '2-1', 'abcd', '1234')
  put_json(tmp_path, 'hud-recovery.json', {
    'updated': 100., 'state': 'running', 'renderer_pid': 123, 'last_error': 'usb_disconnected'})
  result = monitor.sample('jetson', runtime=tmp_path, usb_sys=usb, net_sys=net, now=100.)
  assert result['assessment'] == 'panel_usb_not_enumerated'
  assert result['recovery']['renderer_running'] is True


def test_historical_encoder_error_does_not_override_running_state(tmp_path):
  put_json(tmp_path, 'hud-recovery.json', {
    'updated': 100., 'state': 'running', 'renderer_pid': 123, 'last_error': 'encoder_output_stalled'})
  result = monitor.sample('jetson', runtime=tmp_path, now=100.)
  assert result['assessment'] == 'insufficient_evidence'


def test_current_encoder_error_is_separate_from_usb(tmp_path):
  put_json(tmp_path, 'hud-recovery.json', {
    'updated': 100., 'state': 'retrying', 'renderer_pid': None, 'last_error': 'encoder_output_stalled'})
  result = monitor.sample('jetson', runtime=tmp_path, now=100.)
  assert result['assessment'] == 'encoder_failure_possible'


def test_invalid_json_and_nonfinite_timestamps_are_rejected(tmp_path):
  (tmp_path / 'ncm-display.json').write_text('{"updated": NaN, "state":"connected"}')
  assert monitor.bounded_json(tmp_path / 'ncm-display.json') == {}
  (tmp_path / 'ncm-display.json').write_bytes(b'x' * (monitor.READ_LIMIT + 1))
  assert monitor.bounded_json(tmp_path / 'ncm-display.json') == {}
  assert monitor.age(float('nan'), 100.) is None


def test_transitions_and_counts_do_not_confuse_epochs():
  def row(at, status):
    return {'observed_at_utc': at, 'assessment': status}
  result = monitor.build_summary([
    row('first', 'tcp_session_not_ready'), row('second', 'tcp_session_not_ready'),
    row('third', 'panel_write_recent_not_physical_proof')])
  assert result['samples'] == 3
  assert result['assessment_counts']['tcp_session_not_ready'] == 2
  assert len(result['transitions']) == 2


def test_one_second_soak_writes_bounded_summary(tmp_path):
  result = monitor.run('comma', tmp_path / 'out', duration_s=1, interval_s=.5,
                       runtime=tmp_path, usb_sys=tmp_path / 'missing', net_sys=tmp_path)
  assert result['samples'] >= 2
  samples = (tmp_path / 'out/hud-link-samples.jsonl').read_text().splitlines()
  assert len(samples) == result['samples']
  assert (tmp_path / 'out/hud-link-summary.json').exists()


def test_recent_panel_write_cannot_hide_current_panel_disconnect(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '2-1', 'abcd', '1234')
  put_json(tmp_path, 'ncm-display.json', {'updated': 100., 'state': 'connected', 'hello_complete': True})
  put_json(tmp_path, 'hud-status.json', {'updated': 100., 'last_panel_send_mono': 99.99, 'display_mode': 'vehicle'})
  put_json(tmp_path, 'hud-recovery.json', {'updated': 100., 'state': 'running', 'renderer_pid': 123})
  result = monitor.sample('jetson', runtime=tmp_path, usb_sys=usb, net_sys=net, now=100.)
  assert result['encoder']['writes_recent']
  assert result['assessment'] == 'panel_usb_not_enumerated'


def test_recent_neutral_frames_cannot_hide_current_source_gap(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '1-1', '1cbe', '0092')
  put_json(tmp_path, 'ncm-display.json', {'updated': 100., 'state': 'connected', 'hello_complete': True})
  put_json(tmp_path, 'hud-status.json', {'updated': 100., 'last_panel_send_mono': 99.99, 'display_mode': 'waiting_data'})
  result = monitor.sample('jetson', runtime=tmp_path, usb_sys=usb, net_sys=net, now=100.)
  assert result['encoder']['writes_recent']
  assert result['assessment'] == 'hud_source_data_gap'


def test_recent_usb_write_cannot_hide_current_encoder_failure(tmp_path):
  put_json(tmp_path, 'ncm-display.json', {'updated': 100., 'state': 'connected', 'hello_complete': True})
  put_json(tmp_path, 'hud-status.json', {'updated': 100., 'last_panel_send_mono': 99.99, 'display_mode': 'vehicle'})
  put_json(tmp_path, 'hud-recovery.json', {'updated': 100., 'state': 'retrying', 'last_error': 'encoder_output_stalled'})
  result = monitor.sample('jetson', runtime=tmp_path, now=100.)
  assert result['assessment'] == 'encoder_failure_possible'


def test_incidents_count_entries_not_every_poll():
  def row(code):
    return {'observed_at_utc': '2026-11-02T00:00:00Z', 'assessment': code}
  result = monitor.build_summary([row('tcp_session_not_ready'), row('tcp_session_not_ready'),
                                  row('panel_write_recent_not_physical_proof'),
                                  row('tcp_session_not_ready'), row('insufficient_evidence'),
                                  row('hud_source_data_gap'), row('hud_source_data_gap')])
  assert result['incident_counts'] == {'tcp_session_not_ready': 2, 'hud_source_data_gap': 1}


def test_slow_render_is_not_mistaken_for_panel_usb_disconnect(tmp_path):
  usb, net = tmp_path / 'usb', tmp_path / 'net'
  usb.mkdir()
  net.mkdir()
  usb_node(usb, '1-1', '1cbe', '0092')
  iface_node(net, 1)
  put_json(tmp_path, 'ncm-display.json', {'updated': 100., 'state': 'connected', 'hello_complete': True,
                                         'last_hud_rx': 99.9})
  put_json(tmp_path, 'hud-status.json', {'updated': 100., 'display_mode': 'waiting_data',
                                          'last_panel_send_mono': 99.9, 'render_expired': True,
                                          'encoded_expired_frames': 201})
  put_json(tmp_path, 'hud-input.json', {'updated': 100., 'mode': 'waiting_data',
                                         'last_gap': {'event': 'snapshot_expired', 'reason': 'render_expired',
                                                      'jetson_mono': 99.8}})
  result = monitor.sample('jetson', 'usb0', tmp_path, usb, net, now=100.1)
  assert result['assessment'] == 'render_timeout_wait_frame'
  assert result['tcp']['last_hud_rx_age_s'] == .2
  assert result['input_gap_history'] == {'reason': 'render_expired', 'age_s': .3, 'event': True}
  assert result['panel_usb']['status'] == 'present'
  assert result['physical_hud_verified'] is False


def test_connected_hud_without_recent_panel_write_is_a_suspect_only(tmp_path):
  usb = tmp_path / 'usb'
  usb.mkdir()
  usb_node(usb, '1-1', '1cbe', '0092')
  put_json(tmp_path, 'ncm-display.json', {'updated': 100., 'state': 'connected', 'hello_complete': True})
  put_json(tmp_path, 'hud-status.json', {'updated': 100., 'display_mode': 'vehicle',
                                          'last_panel_send_mono': 98.2, 'render_expired': False})
  put_json(tmp_path, 'hud-recovery.json', {'updated': 100., 'state': 'running', 'renderer_pid': 123})
  result = monitor.sample('jetson', runtime=tmp_path, usb_sys=usb, now=100.)
  assert result['assessment'] == 'panel_write_stalled_possible'
  assert result['physical_hud_verified'] is False


def test_summary_counts_only_fresh_increasing_frame_drops():
  def row(n, status='fresh'):
    return {'observed_at_utc': '2026-11-03T00:00:00Z',
            'assessment': 'panel_write_recent_not_physical_proof',
            'encoder': {'status': status, 'expired_frames_total': n}}
  result = monitor.build_summary([row(185), row(189), row(201), row(201),
                                  row(20), row(22), row(50, 'stale'), row(60)])
  assert result['observed_expired_frames_delta'] == 18
  assert result['intervals_with_frame_drops'] == 3
