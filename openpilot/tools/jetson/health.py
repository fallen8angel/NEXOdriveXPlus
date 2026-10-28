"""Read-only, bounded diagnostic evidence shared by 7000 and long logs."""
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import time

from openpilot.tools.jetson.state import RUNTIME, finite, public_telemetry, read_fresh


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
  return {'comma_revision': source_revision(), 'jetson_revision': peer.get('source_revision'),
          'tcp_state': ncm.get('state', 'unknown') if live else 'unknown',
          'status_fresh': live, 'hello_complete': ncm.get('hello_complete') is True if live else None,
          'connection_anomalies': ncm.get('connection_anomalies'), 'transport_recoveries': ncm.get('transport_recoveries'),
          'last_hud_tx_mono': ncm.get('last_hud_tx_mono'),
          'last_jetson_reply_mono': ncm.get('last_peer_rx_mono'),
          'hud_tx_age_s': age(ncm.get('last_hud_tx_mono')), 'jetson_reply_age_s': peer_age,
          'jetson_hud_rx_age_s': remote_age(peer.get('hud_rx_age')),
          'panel_write_age_s': remote_age(remote.get('last_successful_panel_age_s', remote.get('last_panel_send_age_s'))),
          'renderer_pid': remote.get('renderer_pid'), 'host_pid': peer.get('host_pid'),
          'recovery': recovery, 'camera_shutdown': {k: camera[k] for k in ('updated', 'errors', 'stage') if k in camera},
          'transport_error': ncm.get('error', ''),
          'wire_send': ncm.get('wire_send'),
          'timing': {k: ncm.get(k) for k in ('hud_prepare_ms', 'hud_send_ms', 'hud_send_budget_ms',
                                            'hud_source_age_s', 'snapshot_drop_age_s', 'state_rtt_ms')},
          'receiver_timing': {k: peer.get(k) for k in ('hud_rx_gap_s', 'hud_store_ms')},
          'renderer_timing': {k: remote.get(k) for k in ('snapshot_age_s', 'panel_send_ms', 'render_ms', 'render_expired')},
          'physical_hud_verified': False,
          'clock_note': 'mono fields are comma-local; remote ages include elapsed time since reply; no clock subtraction across devices'}
