"""Read-only 30-minute HUD transport, encoder and USB-path observation.

This does not send traffic, reset either USB link or claim that pixels are visible.
Use on the comma and Jetson separately; their monotonic clocks are not shared.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path
import time

RUNTIME = Path('/dev/shm/nexo-jetson')
USB_SYS = Path('/sys/bus/usb/devices')
NET_SYS = Path('/sys/class/net')
PANEL_IDS = {('1cbe', '0092'), ('1cbe', '0123')}
READ_LIMIT = 131072
FRESH_S = 2.5


def number(value):
  return type(value) in (int, float) and math.isfinite(value)


def age(value, now):
  return round(now - value, 3) if number(value) and 0 <= value <= now else None


def bounded_json(path):
  try:
    with Path(path).open('rb') as source:
      raw = source.read(READ_LIMIT + 1)
    if len(raw) > READ_LIMIT:
      return {}
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
    return {}


def read_text(path, limit=64):
  try:
    with Path(path).open('r', encoding='utf-8') as source:
      return source.read(limit).strip()
  except (OSError, UnicodeError):
    return None


def error_category(value):
  return value if isinstance(value, str) and re.fullmatch('[a-z_]{1,48}', value) else 'unclassified'


def integer(value):
  return value if type(value) is int and value >= 0 else None


def evidence(record, now):
  if not record:
    return {'state': 'missing', 'age_s': None}
  elapsed = age(record.get('updated'), now)
  return {'state': 'fresh' if elapsed is not None and elapsed <= FRESH_S else 'stale', 'age_s': elapsed}


def usb_panel(sys_dir=USB_SYS):
  """USB panel and NCM network are deliberately observed as different paths."""
  devices, readable = [], False
  try:
    entries = list(Path(sys_dir).iterdir())
  except OSError:
    return {'status': 'unknown', 'device_ids': []}
  for entry in entries:
    vid = read_text(entry / 'idVendor', 8)
    pid = read_text(entry / 'idProduct', 8)
    if vid is None or pid is None:
      continue
    readable = True
    pair = (vid.lower(), pid.lower())
    if pair in PANEL_IDS:
      devices.append(f'{pair[0]}:{pair[1]}')
  return {'status': 'present' if devices else 'absent' if readable else 'unknown',
          'device_ids': sorted(set(devices))}


def ncm_interface(iface, sys_dir=NET_SYS):
  # Missing or unselected interface is unknown, not proof of a disconnected cable.
  if not iface or '/' in iface or iface in ('.', '..'):
    return {'status': 'not_selected'}
  root = Path(sys_dir) / iface
  if not root.is_dir():
    return {'status': 'not_found'}
  carrier = read_text(root / 'carrier', 3)
  operstate = read_text(root / 'operstate', 32)
  def bytes_counter(name):
    value = read_text(root / 'statistics' / name, 24)
    try:
      return int(value) if value is not None and int(value) >= 0 else None
    except ValueError:
      return None
  return {'status': 'observed', 'interface': iface, 'carrier': True if carrier == '1' else False if carrier == '0' else None,
          'operstate': operstate, 'rx_bytes': bytes_counter('rx_bytes'), 'tx_bytes': bytes_counter('tx_bytes')}


def sample(role, iface=None, runtime=RUNTIME, usb_sys=USB_SYS, net_sys=NET_SYS, now=None):
  now = time.monotonic() if now is None else now
  ncm = bounded_json(runtime / 'ncm-display.json')
  panel = bounded_json(runtime / 'hud-status.json')
  recovery = bounded_json(runtime / 'hud-recovery.json')
  stage = bounded_json(runtime / 'hud-stage.json')
  input_event = bounded_json(runtime / 'hud-input.json')
  last_gap = input_event.get('last_gap') if isinstance(input_event.get('last_gap'), dict) else {}
  ncm_meta, panel_meta = evidence(ncm, now), evidence(panel, now)
  tcp_state = ncm.get('state') if ncm_meta['state'] == 'fresh' else None
  last_panel_age = age(panel.get('last_panel_send_mono'), now) if panel_meta['state'] == 'fresh' else None
  last_tx_age = age(ncm.get('last_hud_tx_mono'), now) if ncm_meta['state'] == 'fresh' else None
  last_peer_age = age(ncm.get('last_peer_rx_mono'), now) if ncm_meta['state'] == 'fresh' else None
  rec_meta = evidence(recovery, now)
  data = {
    'observed_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'uptime_mono_s': round(now, 3), 'role': role,
    'ncm_usb': ncm_interface(iface, net_sys),
    'panel_usb': usb_panel(usb_sys) if role == 'jetson' else {'status': 'not_observed_on_comma', 'device_ids': []},
    'tcp': {'status': ncm_meta['state'], 'state': tcp_state, 'hello_complete': ncm.get('hello_complete') is True if tcp_state else None,
            'age_s': ncm_meta['age_s'], 'last_peer_rx_age_s': last_peer_age, 'last_hud_tx_age_s': last_tx_age,
            'last_hud_rx_age_s': age(ncm.get('last_hud_rx'), now) if ncm_meta['state'] == 'fresh' else None,
            'anomalies_total': integer(ncm.get('connection_anomalies')), 'recoveries_total': integer(ncm.get('transport_recoveries')),
            'historical_error_recorded': bool(ncm.get('last_transport_error'))},
    'encoder': {'status': panel_meta['state'], 'display_mode': panel.get('display_mode') if panel_meta['state'] == 'fresh' and panel.get('display_mode') in ('vehicle', 'waiting_data', 'parking') else None,
                'last_panel_write_age_s': last_panel_age, 'writes_recent': last_panel_age is not None and last_panel_age <= FRESH_S,
                'render_ms': panel.get('render_ms') if number(panel.get('render_ms')) else None,
                'panel_send_ms': panel.get('panel_send_ms') if number(panel.get('panel_send_ms')) else None,
                'render_expired': panel.get('render_expired') is True if panel_meta['state'] == 'fresh' else None,
                'expired_frames_total': integer(panel.get('encoded_expired_frames')) if panel_meta['state'] == 'fresh' else None},
    'input_gap_history': {'reason': error_category(last_gap.get('reason')) if last_gap.get('reason') else None,
                          'age_s': age(last_gap.get('jetson_mono'), now),
                          'event': last_gap.get('event') == 'snapshot_expired'},
    'recovery': {'status': rec_meta['state'], 'state': recovery.get('state') if rec_meta['state'] == 'fresh' else None,
                 'failure_stage': error_category(recovery.get('failure_stage')) if rec_meta['state'] == 'fresh' else None,
                 'last_error': error_category(recovery.get('last_error')) if rec_meta['state'] == 'fresh' else None,
                 'failures_total': integer(recovery.get('failures')) if rec_meta['state'] == 'fresh' else None,
                 'renderer_running': type(recovery.get('renderer_pid')) is int and recovery['renderer_pid'] > 0 if rec_meta['state'] == 'fresh' else None,
                 'stage': error_category(stage.get('stage')) if evidence(stage, now)['state'] == 'fresh' else None},
    'physical_hud_verified': False,
  }
  data['assessment'] = assess(data)
  return data


def assess(data):
  tcp, network, panel, encoder, recovery = (data[k] for k in ('tcp', 'ncm_usb', 'panel_usb', 'encoder', 'recovery'))
  if network.get('carrier') is False and tcp['status'] == 'fresh' and tcp['state'] != 'connected':
    return 'ncm_usb_link_loss_possible'
  if tcp['status'] == 'fresh' and (tcp['state'] != 'connected' or tcp['hello_complete'] is not True):
    return 'tcp_session_not_ready'
  # Prefer current failure evidence to a still-recent last successful USB write:
  # a panel may have disconnected or an encoder may be restarting after that write.
  if panel['status'] == 'absent' and recovery.get('renderer_running') is True:
    return 'panel_usb_not_enumerated'
  # Last errors are historical: require active failing state, never classify from old error alone.
  if recovery['status'] == 'fresh' and recovery['state'] != 'running':
    reason = str(recovery.get('last_error') or '')
    if 'encoder' in reason:
      return 'encoder_failure_possible'
    if reason.startswith(('usb_', 'panel_')):
      return 'panel_usb_failure_possible'
  # A renderer can continue sending neutral waiting frames while source input is
  # absent. Successful USB writes do not establish fresh vehicle data.
  if encoder['status'] == 'fresh' and encoder['render_expired'] is True:
    return 'render_timeout_wait_frame'
  if encoder['status'] == 'fresh' and encoder['display_mode'] == 'waiting_data':
    return 'hud_source_data_gap'
  if tcp['status'] == 'fresh' and tcp['state'] == 'connected' and tcp['last_hud_tx_age_s'] is not None and tcp['last_hud_tx_age_s'] > FRESH_S:
    return 'comma_hud_producer_gap'
  if (tcp['status'] == 'fresh' and tcp['state'] == 'connected' and encoder['status'] == 'fresh'
      and recovery.get('renderer_running') is True and encoder.get('last_panel_write_age_s') is not None
      and encoder['last_panel_write_age_s'] >= 1.5):
    return 'panel_write_stalled_possible'
  if encoder['writes_recent']:
    return 'panel_write_recent_not_physical_proof'
  return 'insufficient_evidence'


def build_summary(records):
  classes = Counter()
  changes = []
  changes_total, count, prev = 0, 0, None
  incident_counts = Counter()
  observed_expired_frames, intervals_with_frame_drops = 0, 0
  prev_expired = None
  normal = {'panel_write_recent_not_physical_proof', 'insufficient_evidence'}
  for row in records:
    count += 1
    current = row['assessment']
    classes[current] += 1
    encoder = row.get('encoder', {})
    total = encoder.get('expired_frames_total') if encoder.get('status') == 'fresh' else None
    if type(total) is int and type(prev_expired) is int and total > prev_expired:
      observed_expired_frames += total - prev_expired
      intervals_with_frame_drops += 1
    prev_expired = total if type(total) is int else None
    if current != prev:
      changes_total += 1
      if current not in normal:
        incident_counts[current] += 1
      if len(changes) < 256:
        changes.append({'at_utc': row['observed_at_utc'], 'assessment': current})
    prev = current
  return {'samples': count, 'assessment_counts': dict(classes), 'incident_counts': dict(incident_counts),
          'observed_expired_frames_delta': observed_expired_frames,
          'intervals_with_frame_drops': intervals_with_frame_drops,
          'transitions': changes, 'transitions_truncated': changes_total > 256, 'physical_hud_verified': False,
          'interpretation': 'Read-only, independent USB paths. Connected TCP / completed USB writes are not proof of physical HUD pixels. '
                            'A historical transport error is not a currently active failure.'}


def run(role, output, duration_s=1800., interval_s=1., iface=None, runtime=RUNTIME, usb_sys=USB_SYS, net_sys=NET_SYS):
  if not (1 <= duration_s <= 24 * 3600) or not (.2 <= interval_s <= 60):
    raise ValueError('duration must be 1..86400 s and interval .2..60 s')
  output = Path(output)
  output.mkdir(parents=True, exist_ok=True)
  started = time.monotonic()
  interrupted = False
  samples_path = output / 'hud-link-samples.jsonl'
  try:
    with samples_path.open('w', encoding='utf-8') as stream:
      while True:
        now = time.monotonic()
        row = sample(role, iface, runtime, usb_sys, net_sys, now)
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        remaining = duration_s - (time.monotonic() - started)
        if remaining <= 0:
          break
        time.sleep(min(interval_s, remaining))
  except KeyboardInterrupt:
    interrupted = True
  with samples_path.open('r', encoding='utf-8') as stream:
    summary = build_summary(json.loads(line) for line in stream if line.strip())
  summary.update(role=role, duration_s=round(time.monotonic() - started, 1), interrupted=interrupted)
  (output / 'hud-link-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
  return summary


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--role', choices=('comma', 'jetson'), required=True)
  parser.add_argument('--output', type=Path, required=True, help='new directory for JSONL samples and summary')
  parser.add_argument('--seconds', type=float, default=1800., help='default 1800 s = 30 minutes')
  parser.add_argument('--interval', type=float, default=1.)
  parser.add_argument('--ncm-iface', help='explicit USB network interface (comma commonly usb0)')
  args = parser.parse_args()
  result = run(args.role, args.output, args.seconds, args.interval, args.ncm_iface)
  print(json.dumps(result, ensure_ascii=False, indent=2))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
