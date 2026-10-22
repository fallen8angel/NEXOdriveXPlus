"""Bounded, sanitized Jetson link transition history; no network or vehicle writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

HISTORY = Path('/data/carrot/jetson-connection-history.json')
MAX_EVENTS = 80
MAX_FILE_BYTES = 48 * 1024
ALLOWED_CODES = frozenset(('INFERENCE_OFF', 'DISPLAY_CONFLICT', 'NCM_MISSING',
                           'NCM_AMBIGUOUS', 'UDC_NOT_BOUND', 'USB_ADMIN_DOWN',
                           'USB_LINK_DOWN', 'NCM_IP_MISSING', 'NCM_ROUTE_MISSING',
                           'WAITING_FOR_JETSON', 'PEER_BEACON_ONLY', 'NATIVE_READY',
                           'NATIVE_ERROR', 'OTHER_TRANSPORT', 'NCM_LINK_READY', 'UNKNOWN'))


def signature(report):
  usb = report.get('usb_gadget') or {}
  link = report.get('usb_link') or {}
  peer = report.get('jetlink') or {}
  recovery = report.get('network_recovery') or {}
  assessment = report.get('assessment') or {}
  return {'code': str(assessment.get('code') or 'UNKNOWN')[:40],
          'ncm_interface': str(usb.get('ncm_interface') or '')[:16],
          'usb_carrier': usb.get('ncm_carrier') if type(usb.get('ncm_carrier')) is bool else (link.get('usb_carrier') if type(link.get('usb_carrier')) is bool else None),
          'peer_recent': peer.get('peer_telemetry_fresh') is True,
          'native_state': str(peer.get('native_state') or 'unknown')[:36],
          'network_state': str(recovery.get('state') or 'unknown')[:32]}


class LinkHistory:
  def __init__(self, path=HISTORY):
    self.path = Path(path)
    self.events = self._read()
    self._candidate, self._count, self._previous = None, 0, None
    self.persistence_error = ''

  def _read(self):
    try:
      if self.path.is_symlink() or self.path.stat().st_size > MAX_FILE_BYTES:
        return []
      data = json.loads(self.path.read_text(encoding='utf-8'))
      if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('events'), list):
        return []
      events = []
      for row in data['events'][-MAX_EVENTS:]:
        if not isinstance(row, dict) or set(row) != {'at', 'state'} or not isinstance(row['state'], dict):
          continue
        if row['state'].get('code') not in ALLOWED_CODES or not isinstance(row['at'], str):
          continue
        state = {k: row['state'].get(k) for k in ('code', 'ncm_interface', 'usb_carrier',
                                                 'peer_recent', 'native_state', 'network_state')}
        events.append({'at': row['at'][:40], 'state': state})
      return events
    except (OSError, ValueError, TypeError, UnicodeError):
      return []

  def _save(self):
    self.path.parent.mkdir(parents=True, exist_ok=True)
    if self.path.is_symlink() or self.path.parent.is_symlink():
      raise OSError('unsafe history destination')
    temp = self.path.with_name(f'{self.path.name}.{os.getpid()}.tmp')
    try:
      temp.write_text(json.dumps({'version': 1, 'events': self.events[-MAX_EVENTS:]},
                                 ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
      if temp.stat().st_size > MAX_FILE_BYTES:
        raise OSError('history too large')
      os.replace(temp, self.path)
    finally:
      temp.unlink(missing_ok=True)

  def observe(self, report):
    state = signature(report)
    if state['code'] not in ALLOWED_CODES:
      state['code'] = 'UNKNOWN'
    if state != self._candidate:
      self._candidate, self._count = state, 1
      return False
    self._count += 1
    if self._count < 2 or self._previous == state:
      return False
    self._previous = dict(state)
    self.events.append({'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'state': dict(state)})
    self.events = self.events[-MAX_EVENTS:]
    try:
      self._save()
      self.persistence_error = ''
    except OSError:
      self.persistence_error = 'unable to persist; history kept in memory'
    return True

  def export(self):
    return {'version': 1, 'events': list(self.events), 'count': len(self.events),
            'persistence': 'memory-only' if self.persistence_error else 'file',
            'note': '2회 연속 관측된 연결 상태 변화만 기록합니다. 최대 80건을 보관합니다.'}


def history_text(value):
  lines = ['NEXO Jetson 연결 이력 (UTC)', value.get('note', '')]
  for event in value.get('events', []):
    s = event.get('state') or {}
    lines.append(f"{event.get('at', '?')} | {s.get('code', '?')} | NCM={s.get('ncm_interface') or '-'}"
                 f" | carrier={s.get('usb_carrier')} | peer={s.get('peer_recent')}"
                 f" | native={s.get('native_state')} | recovery={s.get('network_state')}")
  return '\n'.join(lines)+'\n'
