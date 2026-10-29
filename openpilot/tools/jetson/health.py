"""Read-only, bounded diagnostic evidence shared by 7000 and long logs."""
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import time

from openpilot.tools.jetson.state import RUNTIME, finite, public_telemetry, public_text, read_fresh


@lru_cache(maxsize=1)
def source_revision():
  root = Path(__file__).resolve().parents[3]
  try:
    stamp = (root / 'SOURCE_COMMIT').read_text().strip()
  except OSError:
    try:
      stamp = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                      timeout=.5, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
      stamp = ''
  return stamp if re.fullmatch('[0-9a-f]{40}', stamp) else None


def diagnostic_evidence(now=None):
  now = time.monotonic() if now is None else now
  ncm = read_fresh(RUNTIME / 'ncm-display.json', float('inf'), now) or {}
  peer = ncm.get('peer') if isinstance(ncm.get('peer'), dict) else {}
  def age(stamp):
    return now - stamp if finite(stamp) and 0 <= stamp <= now else None
  peer_age = age(ncm.get('last_peer_rx_mono'))
  live = age(ncm.get('updated')) is not None and age(ncm['updated']) < 2
  remote = peer.get('hud_pipeline') if isinstance(peer.get('hud_pipeline'), dict) else {}
  def remote_age(value):
    return value + peer_age if live and finite(value) and value >= 0 and peer_age is not None else None
  camera = read_fresh(RUNTIME / 'camera-shutdown.json', float('inf'), now, limit=4096) or {}
  recovery = public_telemetry(peer.get('telemetry')).get('nexo_hud_recovery', {})
  diagnostic = peer.get('hud_diagnostics') if isinstance(peer.get('hud_diagnostics'), dict) else {}
  report = diagnostic.get('recovery') if isinstance(diagnostic.get('recovery'), dict) else {}
  if report:
    recovery = public_telemetry({'nexo_hud_recovery': report})['nexo_hud_recovery']
    recovery.update(available=report.get('available') is True,
                    report_status=public_text(report.get('report_status')),
                    blocked=report.get('blocked') if type(report.get('blocked')) is bool else None,
                    error_type=public_text(report.get('error_type')), error_detail=public_text(report.get('error_detail')))
  elif not recovery:
    recovery = {'available': False, 'report_status': 'not_reported', 'state': 'unknown',
                'blocked': None, 'last_error': None, 'last_error_time_unix': None}
  peer_live = live and ncm.get('state') == 'connected' and peer_age is not None and peer_age < 2
  if not peer_live:
    recovery.update(available=False, report_status='peer_unavailable')
  running = remote.get('renderer_running') if peer_live else None
  output_age = remote_age(remote.get('last_successful_panel_age_s', remote.get('last_panel_send_age_s')))
  output_recent = peer_live and remote.get('panel_write_recent') is True and output_age is not None and output_age < 2
  rx_age = remote_age(peer.get('hud_rx_age'))
  tx_age = age(ncm.get('last_hud_tx_mono'))
  stopped_stage = ('tcp' if not peer_live else 'snapshot_producer' if tx_age is None or tx_age >= .5 else
                   'host_snapshot' if rx_age is None or rx_age >= .5 else
                   'none' if output_recent else 'hud_service' if report and report.get('available') is not True else
                   (public_text(recovery.get('failure_stage')) if running is False else '') or
                   public_text(remote.get('renderer_stage')) or 'renderer_not_reported')
  service = diagnostic.get('service') if isinstance(diagnostic.get('service'), dict) else {}
  safe_service = {k: public_text(service[k], 64) for k in ('ActiveState', 'SubState', 'Result', 'ExecMainCode',
                    'ExecMainStatus', 'MainPID', 'NRestarts') if k in service}
  safe_service['available'] = peer_live and service.get('available') is True
  return {'comma_revision': source_revision(), 'jetson_revision': peer.get('source_revision'),
          'tcp_state': ncm.get('state', 'unknown') if live else 'unknown',
          'status_fresh': live, 'hello_complete': ncm.get('hello_complete') is True if live else None,
          'connection_anomalies': ncm.get('connection_anomalies'), 'transport_recoveries': ncm.get('transport_recoveries'),
          'last_hud_tx_mono': ncm.get('last_hud_tx_mono'),
          'last_jetson_reply_mono': ncm.get('last_peer_rx_mono'),
          'hud_tx_age_s': age(ncm.get('last_hud_tx_mono')), 'jetson_reply_age_s': peer_age,
          'jetson_hud_rx_age_s': remote_age(peer.get('hud_rx_age')),
          'panel_write_age_s': remote_age(remote.get('last_successful_panel_age_s', remote.get('last_panel_send_age_s'))),
          'renderer_pid': remote.get('renderer_pid') if peer_live else None, 'host_pid': peer.get('host_pid'),
          'renderer_running': running, 'renderer_stage': public_text(remote.get('renderer_stage')) if peer_live else 'unknown',
          'panel_write_reported': output_recent, 'stopped_stage': stopped_stage, 'hud_service': safe_service,
          'last_error_time_unix': recovery.get('last_error_time_unix'),
          'recovery': recovery, 'camera_shutdown': {k: camera[k] for k in ('updated', 'errors', 'stage') if k in camera},
          'transport_error': ncm.get('error', ''),
          'last_transport_error': ncm.get('last_transport_error'),
          'wire_send': ncm.get('wire_send'),
          'timing': {k: ncm.get(k) for k in ('hud_prepare_ms', 'hud_send_ms', 'hud_send_budget_ms',
                                            'hud_source_age_s', 'snapshot_drop_age_s', 'state_rtt_ms')},
          'receiver_timing': {k: peer.get(k) for k in ('hud_rx_gap_s', 'hud_store_ms')},
          'renderer_timing': {k: remote.get(k) for k in ('snapshot_age_s', 'panel_send_ms', 'render_ms', 'render_expired')},
          'physical_hud_verified': False,
          'clock_note': 'mono fields are comma-local; remote ages include elapsed time since reply; no clock subtraction across devices'}
