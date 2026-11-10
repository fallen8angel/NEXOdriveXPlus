"""Bounded, read-only one-click diagnostic ZIP for the 7000 Jetson page.

The caller supplies snapshots already collected by the existing diagnostics.
No network connection, vehicle state change, or Jetson action happens here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile


MAX_ENTRY_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
PRIVATE_FIELD = re.compile(r'(csrf|password|passwd|secret|token|authorization|private.?key|ssh_target|credential|psk)', re.I)


def redact(value, depth=0):
  """Avoid exporting browser confirmation tokens or credentials with status snapshots."""
  if depth > 16:
    return '[중첩 깊이 제한]'
  if isinstance(value, dict):
    return {str(key): '[보안상 제외]' if PRIVATE_FIELD.search(str(key)) else redact(item, depth + 1)
            for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [redact(item, depth + 1) for item in value[:2000]]
  if isinstance(value, str) and len(value) > MAX_ENTRY_BYTES:
    return value[:MAX_ENTRY_BYTES] + '\n[크기 제한으로 생략됨]'
  return value


def make_bundle(files: dict, failures: dict, generated_at: datetime | None = None) -> bytes:
  """Build a single ZIP; individual diagnostics may fail without losing other files."""
  checked = generated_at or datetime.now(timezone.utc)
  checked = checked.astimezone(timezone.utc)
  accepted = {}
  unavailable = {str(key): str(reason)[:400] for key, reason in failures.items()}
  for name, value in files.items():
    # All names are supplied by our fixed route, never by an HTTP parameter.
    if not re.fullmatch(r'[a-z0-9_-]+(?:/[a-z0-9_-]+)?\.(?:json|txt)', name):
      raise ValueError('invalid diagnostic archive entry')
    if name.endswith('.json'):
      payload = (json.dumps(redact(value), ensure_ascii=False, indent=2, default=str, allow_nan=False) + '\n').encode('utf-8')
    else:
      payload = str(value).encode('utf-8')
    if len(payload) > MAX_ENTRY_BYTES:
      unavailable[name] = f'자료가 {MAX_ENTRY_BYTES}바이트 제한을 초과하여 제외됨'
      continue
    accepted[name] = payload
  if sum(map(len, accepted.values())) > MAX_TOTAL_BYTES:
    raise ValueError('diagnostic archive exceeds size limit')

  manifest = {'schema_version': 1, 'generated_at_utc': checked.isoformat(),
              'read_only': True, 'files': sorted(accepted), 'unavailable_or_failed': unavailable,
              'notice': ('USB carrier, 영상 패킷, JPEG 디코딩, 추론, 외부 HUD 물리 출력은 서로 다른 증거입니다. '
                         '관측되지 않은 기능을 정상이라고 판단하지 않습니다.')}
  summary = [
    'NEXO XPlus · Jetson 통합 진단 (읽기 전용)',
    '수집 시각(UTC): ' + checked.isoformat(),
    '포함 자료: ' + str(len(accepted)) + '개',
    '미수집 자료: ' + str(len(unavailable)) + '개',
    '',
    '파일별 진단값을 종합하여 확인하십시오. 단일 "OK" 값만으로 실제 HUD 화면을 검증할 수 없습니다.',
    'SSH를 설정하지 않았거나 젯슨이 응답하지 않으면 로컬 콤마 진단은 계속 저장합니다.',
    '진단 기능은 Jetlink 추론, 차량 제어, USB 네트워크, 시스템 서비스를 변경하지 않습니다.',
    '',
    '[포함된 파일]', *sorted(accepted), '', '[미수집/확인 불가]'
  ]
  summary.extend(f'{key}: {reason}' for key, reason in sorted(unavailable.items()))
  if not unavailable:
    summary.append('없음')
  output = BytesIO()
  with ZipFile(output, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
    archive.writestr('00_guide.txt', '\n'.join(summary) + '\n')
    archive.writestr('00_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    for name, payload in sorted(accepted.items()):
      archive.writestr(name, payload)
  return output.getvalue()
