"""Read-only service observation outside the TCP receiver's critical path."""
import subprocess
import threading
import time

from openpilot.tools.jetson.state import RUNTIME, atomic_json, finite, public_text, read_fresh

PROPERTIES = ('ActiveState', 'SubState', 'Result', 'ExecMainCode', 'ExecMainStatus', 'MainPID', 'NRestarts')


def service_state():
  try:
    result = subprocess.run(['systemctl', 'show', 'carrot-jetlink-hud.service',
                             '--property=' + ','.join(PROPERTIES)],
                            capture_output=True, text=True, timeout=1, check=True)
    fields = dict(line.split('=', 1) for line in result.stdout[:4096].splitlines() if '=' in line)
    return {'available': True, **{k: public_text(fields[k], 64) for k in PROPERTIES if k in fields}}
  except (OSError, subprocess.SubprocessError, ValueError):
    return {'available': False, 'reason': 'systemd_observation_unavailable'}


class ServiceMonitor:
  def __init__(self):
    self.stop = threading.Event()
    self.thread = threading.Thread(target=self.run, name='hud-service-observer', daemon=True)

  def run(self):
    while not self.stop.is_set():
      value = service_state()
      try:
        atomic_json(RUNTIME / 'hud-service.json', {'updated': time.monotonic(), **value})
      except OSError:
        pass
      self.stop.wait(2)

  def __enter__(self):
    self.thread.start()
    return self

  def __exit__(self, *args):
    self.stop.set()
    self.thread.join(timeout=2)


def renderer_evidence(now, directory=RUNTIME):
  recovery = read_fresh(directory / 'hud-recovery.json', float('inf'), now) or {}
  live = bool(recovery) and now - recovery['updated'] < 2
  service = read_fresh(directory / 'hud-service.json', 5, now) or {'available': False}
  error = read_fresh(directory / 'hud-service-error.json', float('inf'), now) or {}
  stage = read_fresh(directory / 'hud-stage.json', float('inf'), now) or {}
  pid = recovery.get('renderer_pid') if live else None
  running = type(pid) is int and pid > 0
  stage_matches = running and stage.get('pid') == pid
  report = {'available': live, 'report_status': 'fresh' if live else 'stale' if recovery else 'missing',
            'last_error': recovery.get('last_error') or None,
            'state': recovery.get('state') if live else 'supervisor_unavailable',
            'blocked': recovery.get('blocked') if live else None,
            **{k: recovery[k] for k in ('attempts', 'failures', 'successes', 'consecutive_failures', 'cooldown_s',
                'last_error_time_unix', 'failure_stage', 'timed_probes', 'automatic_probes_remaining') if k in recovery}}
  if not live:
    report['last_error'] = error.get('reason') or ('hud_service_failed' if service.get('ActiveState') == 'failed'
                                                else 'supervisor_report_' + report['report_status'])
    report['failure_stage'] = error.get('stage') or 'hud_service'
    if finite(error.get('error_time_unix')):
      report['last_error_time_unix'] = error['error_time_unix']
  fault = stage if stage.get('reason') == report['last_error'] else error if not live else {}
  report['error_type'] = public_text(fault.get('error_type'))
  report['error_detail'] = public_text(fault.get('detail'))
  last_panel = recovery.get('last_successful_panel_mono')
  return {'recovery': report, 'service': service, 'renderer_running': running if live else None,
          'renderer_pid': pid, 'renderer_stage': (stage.get('stage') if stage_matches else recovery.get('renderer_stage'))
             if live else 'hud_service',
          'renderer_error_type': public_text(stage.get('error_type')) if stage_matches else public_text(error.get('error_type')),
          'last_successful_panel_age_s': now - last_panel if finite(last_panel) and 0 <= last_panel <= now else None}
