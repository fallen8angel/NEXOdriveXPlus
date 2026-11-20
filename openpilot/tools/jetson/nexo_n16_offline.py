"""Stage a pinned Carrot N16 ONNX for OFFROAD research, never for driving.

This module cannot enable N16, publish a TensorRT engine, alter production
modeld or forward model outputs to CAN/HUD. Staging is not GPU inference.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from openpilot.tools.jetson import nexo_n16_registry as registry

CACHE_ROOT = Path('/opt/carrot-jetlink/cache/nexo-n16-offline')
# A large research model must not exhaust Jetson storage needed by HUD/recovery.
RESERVE_BYTES = 2 * 1024 ** 3
CHUNK_SIZE = 4 * 1024 * 1024


class StagingError(RuntimeError):
  pass


def _source_ok(path: Path, expected_bytes: int) -> bool:
  if path.is_symlink() or not path.is_file():
    return False
  try:
    return path.stat().st_size == expected_bytes
  except OSError:
    return False


def staged_path(root: Path, sha256: str) -> Path:
  return root / ('carrot-n16-' + sha256[:16] + '.onnx')


def inspect(source: Path | None = None, *, root: Path = CACHE_ROOT) -> dict:
  spec, profile = registry.load_registry()
  expected, size = spec['sha256'], spec['nbytes']
  path = staged_path(root, expected)
  result = {
    'model': 'Carrot Jetlink N16',
    'vehicle': profile['vehicle'],
    'sha256_expected': expected,
    'bytes_expected': size,
    'stage_path': str(path),
    'mode': 'OFFROAD_RESEARCH_ONLY',
    'vehicle_control_enabled': False,
    'modeld_changed': False,
    'hud_prediction_enabled': False,
    'gpu_inference_verified': False,
    'model_staged': False,
  }
  if source is not None:
    result['source_present'] = _source_ok(source, size)
    result['source_sha_verified'] = (registry.hash_matches(source, sha256=expected, nbytes=size)
                                     if result['source_present'] else False)
  result['model_staged'] = (registry.hash_matches(path, sha256=expected, nbytes=size)
                            if _source_ok(path, size) else False)
  result['state'] = ('staged_not_active' if result['model_staged'] else
                     'source_verified_not_staged' if result.get('source_sha_verified') else
                     'source_missing_or_mismatched' if source is not None else
                     'not_staged')
  return result


def stage(source: Path, guard, *, root: Path = CACHE_ROOT, reserve: int = RESERVE_BYTES) -> dict:
  """Copy only an exact, already-owned N16 file into isolated research cache.

  The guard verifies Comma4 Offroad=1, Onroad=0 and external AI OFF.
  Never accept P gear or a local flag as proof of Offroad.
  """
  spec, _ = registry.load_registry()
  expected, size = spec['sha256'], spec['nbytes']
  if not source.is_absolute() or source.is_symlink() or not _source_ok(source, size):
    raise StagingError('N16 원본의 절대 경로·크기·링크 여부를 확인해 주세요.')
  if guard() is not True:
    raise StagingError('콤마4 완전 Offroad 및 외부 AI OFF를 확인하지 못했습니다.')
  if root.is_symlink() or (root.exists() and not root.is_dir()):
    raise StagingError('격리된 연구 모델 폴더가 안전하지 않습니다.')
  root.mkdir(mode=0o700, parents=True, exist_ok=True)
  if root.is_symlink():
    raise StagingError('연구 모델 폴더가 링크로 바뀌었습니다.')
  target = staged_path(root, expected)
  lock_path = root / '.n16-stage.lock'
  if lock_path.is_symlink():
    raise StagingError('모델 작업 잠금 파일이 링크입니다.')
  with lock_path.open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if target.is_symlink():
      raise StagingError('기존 N16 파일이 링크입니다.')
    if target.exists():
      if not registry.hash_matches(target, sha256=expected, nbytes=size):
        raise StagingError('기존 연구 모델이 불일치합니다. 자동 교체하지 않습니다.')
      if guard() is not True:
        raise StagingError('콤마4 완전 Offroad 상태가 해제되었습니다.')
      return {**inspect(root=root), 'action': 'already_staged'}
    if shutil.disk_usage(root).free - size < reserve:
      raise StagingError('모델 복사 후 2GiB 이상 여유 공간이 필요합니다. 기존 파일은 보존합니다.')
    if not registry.hash_matches(source, sha256=expected, nbytes=size):
      raise StagingError('N16 원본의 SHA256이 당근 공식 규격과 다릅니다.')
    fd, name = tempfile.mkstemp(prefix='.n16-stage-', suffix='.part', dir=root)
    tmp = Path(name)
    try:
      os.fchmod(fd, 0o600)
      digest = hashlib.sha256()
      total = 0
      with os.fdopen(fd, 'wb') as dest, source.open('rb') as src:
        while chunk := src.read(CHUNK_SIZE):
          if total % (32 * 1024 * 1024) == 0 and guard() is not True:
            raise StagingError('복사 중 콤마4 Offroad 안전 조건이 해제되었습니다.')
          total += len(chunk)
          if total > size:
            raise StagingError('복사 중 모델 크기가 바뀌었습니다.')
          digest.update(chunk)
          dest.write(chunk)
        dest.flush()
        os.fsync(dest.fileno())
      if total != size or digest.hexdigest() != expected:
        raise StagingError('복사 결과 SHA256이 일치하지 않습니다.')
      if guard() is not True:
        raise StagingError('모델 등록 전에 Offroad 안전 확인이 해제되었습니다.')
      if target.exists() or target.is_symlink():
        raise StagingError('기존 연구 모델을 덮어쓰지 않습니다.')
      os.link(tmp, target, follow_symlinks=False)
      dir_fd = os.open(root, os.O_DIRECTORY | os.O_RDONLY)
      try:
        os.fsync(dir_fd)
      finally:
        os.close(dir_fd)
      return {**inspect(root=root), 'action': 'staged',
              'note': 'N16 연구용 별도 보관 완료. 엔진 생성/추론/주행 활성화 미수행'}
    finally:
      tmp.unlink(missing_ok=True)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--model', type=Path, help='이미 보유한 당근 N16 원본 ONNX 절대 경로')
  parser.add_argument('--stage', action='store_true', help='OFFROAD 확인 후 별도 연구 캐시에 복사')
  parser.add_argument('--comma-ssh', default=None, help='젯슨에서 인증된 comma@192.168.100.127')
  parser.add_argument('--identity-file', default=None, help='기존 승인 SSH 개인키 경로 (선택)')
  args = parser.parse_args()
  try:
    if not args.stage:
      value = inspect(args.model)
    else:
      if args.model is None or args.comma_ssh is None:
        parser.error('--stage에는 --model과 --comma-ssh가 모두 필요합니다.')
      if not Path('/etc/nv_tegra_release').is_file():
        raise StagingError('젯슨 장치가 아닙니다. 이 환경에서는 모델을 배치하지 않습니다.')
      from openpilot.tools.jetson.ssh_preflight import CommaGuard
      from openpilot.tools.jetson.ssh_preflight import ROOT as HOST_ROOT
      guard = CommaGuard(args.comma_ssh, args.identity_file, root=HOST_ROOT)
      value = stage(args.model, guard)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get('model_staged') or value.get('source_sha_verified') else 2
  except (OSError, ValueError, StagingError) as error:
    print(json.dumps({'state': 'blocked', 'reason': str(error)[:350],
                      'vehicle_control_enabled': False, 'modeld_changed': False}, ensure_ascii=False))
    return 1


if __name__ == '__main__':
  raise SystemExit(main())
