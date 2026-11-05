"""Best-effort Comma -> Jetson wall-clock correction for a battery-less HUD host.

The Jetson may boot with a stale 2025 timestamp after power removal. Use only
the Comma's trusted wall clock, the user's already-authorized strict SSH host
key, and sudo date. This never edits any vehicle/CAN/Params values and never
changes Comma's clock. The caller must gate correction on stationary P/Offroad.
"""
from __future__ import annotations

import re
import subprocess
import time

MIN_TRUSTED_COMMA_EPOCH = 1767225600  # 2026-01-01 UTC; reject uninitialized Comma RTC.
MAX_DRIFT_S = 30
SSH_TIMEOUT_S = 12
TARGET = re.compile(r'(?:[a-z_][a-z0-9_-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.:-]{0,126}\Z')


def _ssh(target: str, remote_command: str, *, run=subprocess.run):
  cmd = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
         '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=3',
         '-o', 'ServerAliveCountMax=2', '--', target, remote_command]
  response = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, errors='replace', check=False, timeout=SSH_TIMEOUT_S)
  if response.returncode:
    raise RuntimeError('Jetson SSH 또는 sudo 시각 동기화 실패: ' + (response.stderr or '')[-180:])
  result = (response.stdout or '').strip()
  if not result.isdecimal():
    raise ValueError('Jetson 시각 응답이 숫자가 아닙니다.')
  return int(result)


def sync_once(target: str, *, now=time.time, run=subprocess.run, ready=None):
  """Return a bounded audit result; do not step clocks without fresh P proof."""
  if not isinstance(target, str) or not TARGET.fullmatch(target):
    return {'ok': False, 'status': 'invalid_target'}
  source = int(now())
  if source < MIN_TRUSTED_COMMA_EPOCH:
    return {'ok': False, 'status': 'comma_time_untrusted'}
  if ready is None:
    return {'ok': False, 'status': 'park_proof_required'}

  approved, reason = ready()
  if not approved:
    return {'ok': False, 'status': 'not_parked', 'reason': str(reason)[:140]}

  try:
    remote_epoch = _ssh(target, 'date -u +%s', run=run)
    current_source = int(now())
    drift_s = current_source - remote_epoch
    if abs(drift_s) <= MAX_DRIFT_S:
      return {'ok': True, 'status': 'in_sync', 'drift_s': drift_s}
    # Revalidate actual CAN-based stationary P, not an old HTTP button state.
    approved, reason = ready()
    if not approved:
      return {'ok': False, 'status': 'park_changed', 'reason': str(reason)[:140]}
    target_epoch = int(now())
    if target_epoch < MIN_TRUSTED_COMMA_EPOCH:
      return {'ok': False, 'status': 'comma_time_untrusted'}
    synced_epoch = _ssh(target, 'sudo -n date -u -s @' + str(target_epoch) +
                         ' >/dev/null && date -u +%s', run=run)
    final_drift_s = int(now()) - synced_epoch
    if abs(final_drift_s) > MAX_DRIFT_S:
      return {'ok': False, 'status': 'verify_failed', 'drift_s': final_drift_s}
    return {'ok': True, 'status': 'updated', 'old_drift_s': drift_s,
            'drift_s': final_drift_s}
  except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
    return {'ok': False, 'status': 'sync_failed', 'reason': str(error)[-180:]}
