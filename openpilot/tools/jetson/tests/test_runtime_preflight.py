"""Missing deployment dependencies and service prerequisites must fail before downtime."""
import json
from pathlib import Path
import subprocess
import hashlib

import pytest

from openpilot.tools.jetson import protected_ncm as ncm, runtime_audit, hud, hud_diagnostics, supervisor
from openpilot.tools.jetson.state import atomic_json


def test_windows_package_checksums_have_unix_line_endings_and_no_self_reference(tmp_path):
  from openpilot.tools.jetson.build_hud_update import write_checksums
  (tmp_path / 'payload.tar.gz').write_bytes(b'payload')
  expected = hashlib.sha256(b'payload').hexdigest().encode() + b'  payload.tar.gz\n'
  write_checksums(tmp_path)
  assert (tmp_path / 'SHA256SUMS').read_bytes() == expected
  write_checksums(tmp_path)
  assert (tmp_path / 'SHA256SUMS').read_bytes() == expected


@pytest.mark.parametrize('missing', ['openpilot/common/usbgpu_bus_lock.py', 'tools/jetlink/xorg_auth.py',
                                    'carrot-server/tools/jetlink/boot_display.py'])
def test_export_rejects_missing_boot_and_xorg_dependencies(missing):
  files = dict.fromkeys(runtime_audit.REQUIRED, b'')
  del files[missing]
  with pytest.raises(ValueError, match=missing):
    runtime_audit.audit_files(files)


def service_fixture(monkeypatch, tmp_path, condition='ConditionPathExists=/opt/carrot-jetlink/current/ready'):
  (tmp_path / 'ready').touch()
  script = tmp_path / 'tools/jetlink/xorg_auth.py'
  script.parent.mkdir(parents=True)
  script.touch()
  calls = []
  def run(*args, **kwargs):
    calls.append(args)
    if args[0] == 'systemd-analyze':
      for entry in args[2:]:
        if entry.startswith('ConditionPathExists=') and not entry.startswith('ConditionPathExists=|') and not Path(entry.split('=', 1)[1]).exists():
          raise subprocess.CalledProcessError(1, args, output='path does not exist')
      return 'Conditions succeeded'
    if args[1] == 'cat':
      return ('[Unit]\n' + condition + '\n[Service]\n'
              'ExecStartPre=/usr/bin/python3 /opt/carrot-jetlink/current/tools/jetlink/xorg_auth.py jetlink\n')
    if 'LoadState' in args:
      return 'loaded'
    if args[2] == 'carrot-jetlink-hud.service':
      return 'carrot-protected-storage.service carrot-jetlink-xorg.service'
    return ''
  monkeypatch.setattr(ncm, 'run', run)
  return calls, script


def test_current_path_conditions_project_to_candidate_and_dependencies_stay(monkeypatch, tmp_path):
  calls, _ = service_fixture(monkeypatch, tmp_path)
  assert ncm.XORG_UNIT in ncm.check_service_prerequisites(tmp_path)
  assert any(args[:2] == ('systemd-analyze', 'condition') for args in calls)
  assert not any('start' in args or 'stop' in args for args in calls)
  (tmp_path / 'ready').unlink()
  with pytest.raises(RuntimeError, match='failed start conditions'):
    ncm.check_service_prerequisites(tmp_path)


def test_missing_xorg_helper_is_detected_even_when_service_currently_active(monkeypatch, tmp_path):
  _, script = service_fixture(monkeypatch, tmp_path)
  script.unlink()
  with pytest.raises(ValueError, match='candidate entry point missing'):
    ncm.check_service_prerequisites(tmp_path)


def test_rollback_older_nexo_xorg_helper_uses_ram_without_removing_dependencies(monkeypatch, tmp_path):
  root, ram = tmp_path / 'runtime', tmp_path / 'run'
  monkeypatch.setattr(ncm, 'RUN', ram)
  helper = root / 'current/carrot-server/tools/jetlink/xorg_auth.py'
  helper.parent.mkdir(parents=True)
  helper.touch()
  (root / 'current/NEXO_RUNTIME.json').write_text('{}')
  original = '[Service]\nExecStartPre=/usr/bin/python3 /opt/carrot-jetlink/current/tools/jetlink/xorg_auth.py jetlink\n'
  monkeypatch.setattr(ncm, 'run', lambda *a: original)
  ncm.configure_xorg(root)
  path = next(iter(ncm.xorg_files(root)))
  assert '/current/carrot-server/tools/jetlink/xorg_auth.py jetlink' in path.read_text()
  assert 'Requires=' not in path.read_text() and 'Condition' not in path.read_text()
  monkeypatch.setattr(ncm, 'run', lambda *a: original + 'ExecStartPre=/usr/bin/unknown-safety-check\n')
  with pytest.raises(ValueError, match='Unknown Xorg'):
    ncm.configure_xorg(root)
  ncm.configure_xorg(root, enabled=False)
  assert not path.exists() and helper.exists()


def test_systemd_resets_and_trigger_conditions_remain_grouped(monkeypatch, tmp_path):
  conditions = 'ConditionPathExists=/old\nConditionFileNotEmpty=/also-old\nConditionPathExists=\nConditionPathExists=|/a\nConditionFileNotEmpty=|/b'
  calls, _ = service_fixture(monkeypatch, tmp_path, conditions)
  ncm.check_service_prerequisites(tmp_path)
  assert ('systemd-analyze', 'condition', 'ConditionPathExists=|/a', 'ConditionFileNotEmpty=|/b') in calls


def test_missing_module_and_service_condition_are_distinct(tmp_path):
  assert hud.failure_reason(ModuleNotFoundError('missing module')) == 'renderer_import_failed'
  atomic_json(tmp_path / 'hud-service.json', {'updated': 100., 'available': True,
                                           'ActiveState': 'inactive', 'ConditionResult': 'no'})
  report = hud_diagnostics.renderer_evidence(100., tmp_path)
  assert report['recovery']['last_error'] == 'hud_service_condition_failed'
  assert report['renderer_running'] is None


def test_repaired_release_gets_one_probe_but_same_release_keeps_block(monkeypatch, tmp_path):
  monkeypatch.setattr(supervisor, 'RUNTIME', tmp_path)
  monkeypatch.setattr(supervisor, 'source_identity', lambda: 'new-commit')
  record = {'updated': 100., 'consecutive_failures': 3, 'source_commit': 'old-commit',
            'retry_after': 900., 'rearm': False, 'last_error': 'renderer_import_failed'}
  atomic_json(tmp_path / 'hud-recovery.json', record)
  obj = supervisor.RendererSupervisor([], lambda: True, clock=lambda: 100.)
  assert obj.rearm and obj.retry_after == 100.
  record['source_commit'] = 'new-commit'
  atomic_json(tmp_path / 'hud-recovery.json', record)
  obj = supervisor.RendererSupervisor([], lambda: True, clock=lambda: 100.)
  assert not obj.rearm and obj.retry_after == 900.
