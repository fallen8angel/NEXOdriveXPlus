"""One-click Jetson diagnostic ZIP: shape, redaction, partial failure, limits."""
from datetime import datetime, timezone
from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from openpilot.selfdrive.carrot.server.services.jetson_bundle import MAX_ENTRY_BYTES, make_bundle


def test_bundle_one_file_contains_local_and_remote_sections():
  payload = make_bundle({
    '01_comma_status.json': {'connected': False, 'csrf': 'never-export', 'nested': {'ssh_target': 'secret@ip'}},
    '02_usb_diagnostics.txt': 'USB 상태 확인 불가',
    '03_connection_history.json': {'events': [{'state': {'code': 'INFERENCE_OFF'}}]},
    'remote/05_video_test.json': {'video_received': False, 'note': 'Pillow missing'},
    'remote/07_logs.txt': 'system log',
  }, {'remote/06_model.json': 'SSH unavailable'}, datetime(2026, 11, 10, tzinfo=timezone.utc))

  with ZipFile(BytesIO(payload)) as archive:
    names = set(archive.namelist())
    assert '00_guide.txt' in names and '00_manifest.json' in names
    assert 'remote/05_video_test.json' in names and 'remote/07_logs.txt' in names
    status = json.loads(archive.read('01_comma_status.json'))
    assert status['csrf'] == '[보안상 제외]'
    assert status['nested']['ssh_target'] == '[보안상 제외]'
    assert status['connected'] is False
    manifest = json.loads(archive.read('00_manifest.json'))
    assert manifest['read_only'] is True
    assert 'remote/06_model.json' in manifest['unavailable_or_failed']
    assert 'remote/06_model.json' not in names
    assert b'SSH unavailable' in archive.read('00_guide.txt')


def test_bundle_rejects_untrusted_archive_member_name():
  with pytest.raises(ValueError, match='invalid'):
    make_bundle({'../unexpected.json': {}}, {})


def test_bundle_rejects_oversized_entry_but_preserves_other_files():
  payload = make_bundle({'remote/07_logs.txt': 'x' * (MAX_ENTRY_BYTES + 1),
                         '02_usb_diagnostics.json': {'ok': True}}, {})
  with ZipFile(BytesIO(payload)) as archive:
    assert 'remote/07_logs.txt' not in archive.namelist()
    assert '02_usb_diagnostics.json' in archive.namelist()
    manifest = json.loads(archive.read('00_manifest.json'))
    assert 'remote/07_logs.txt' in manifest['unavailable_or_failed']
