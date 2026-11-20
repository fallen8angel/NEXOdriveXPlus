"""Fixed, bounded Jetson management commands; also sent through keyed SSH stdin.

No install, update application, disk erase, shutdown or arbitrary shell command.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

MANIFEST_URL = 'https://upload.shind0.synology.me/models/jetlink-host-stable/manifest.json'

SERVICES = ('carrot-jetlink.service', 'carrot-jetlink-hud.service', 'nexo-jetson-host.service', 'nexo-jetson-hud.service',
            'nexo-jetlink-server.service', 'nexo-yolo.service', 'jetlink-server.service')
ACTIONS = ('status', 'logs', 'settings', 'model', 'check_update', 'restart', 'reconnect', 'video_test',
           'hud_trace', 'performance', 'performance_history', 'inference_readiness', 'engine_experiments', 'ssh_diagnostics', 'engine_candidates', 'n16_trial_status')


def candidate_logs(root):
  """Read bounded receipts only. No CUDA/ORT imports, validation, registration or activation."""
  root = Path(root)
  report = record(root / 'cache/nexo-engine-validation.json', 1024 * 1024)
  value = report
  stage = Path(str(report.get('stage_directory') or ''))
  if (report.get('state') == 'running' and stage.parent == root / 'cache' and stage.name.startswith('nexo-validation-')
      and stage.resolve() == stage.absolute() and not (stage / 'result.json').is_symlink()):
    value = record(stage / 'result.json', 1024 * 1024) or report
  elif isinstance(report.get('candidate'), dict):
    value = report['candidate']
  keys = ('state', 'phase', 'failed_phase', 'failure_code', 'error', 'source_model_sha256', 'source_commit', 'runtime', 'gpu_device',
          'artifact_sha256', 'numerical_validation_passed', 'gpu_synthetic_verified', 'policy', 'case_count',
          'dataset_scope', 'worst_by_slice', 'reference', 'candidate_cpu_reference', 'timing', 'synthetic_fps',
          'memory_before', 'memory_after', 'numerical_equivalence_verified')
  receipts = []
  registry = root / 'cache/engine-candidates'
  if registry.is_dir() and not registry.is_symlink():
    # islice bounds traversal as well as output; avoid sorting an unbounded directory.
    import itertools
    for path in itertools.islice(registry.iterdir(), 8):
      if path.is_symlink() or not re.fullmatch('[a-f0-9]{64}\\.json', path.name):
        continue
      entry = record(path, 16384)
      if entry.get('active') is False and entry.get('vehicle_approved') is False:
        receipts.append({key: entry.get(key) for key in ('candidate_id', 'status', 'source_model_sha256',
                         'artifact_sha256', 'source_commit', 'active', 'vehicle_approved', 'recorded_driving_verified')})
  return {'ok': True, 'engine_candidate': {'available': bool(report), 'supervisor_state': report.get('state', 'not_run'),
          'validation': {key: value[key] for key in keys if key in value}, 'supervisor_error': report.get('error'),
          'disk_budget': report.get('disk_budget'),
          'registered_candidates': receipts, 'registry_listing_limit': 8,
          'artifact_integrity_rechecked': False, 'recorded_evidence_only': True,
          'production_cache_published': False, 'driving_activation_performed': False,
          'vehicle_approved': False, 'active': False,
          'note': '원본 ONNX CPU / 후보 ONNX CPU / 기존 후보 GPU의 동일 입력 비교 기록입니다. ' +
                  '합성 정확도 통과와 후보 등록은 실제 주행 검증·차량 제어 승인·AI 활성화가 아닙니다.'},
          'summary_text': '\n'.join([
            'NEXO GELU FP32 후보 검증 · 기록 조회만 수행',
            '감독 상태: ' + str(report.get('state') or '미수행'),
            '검증 단계: ' + str(value.get('failed_phase') or value.get('phase') or '미수행'),
            '정확도: ' + ('기준 통과 · 합성 입력 한정' if value.get('numerical_validation_passed') is True else '미완료 또는 기준 미통과'),
            'GPU 합성 비교: ' + ('완료 기록 있음' if value.get('gpu_synthetic_verified') is True else '미검증'),
            'CPU 기준: ' + str(value.get('reference') or '미검증'),
            '평균/p50/p95/최대 ms: ' + str((value.get('timing') or {}).get('synchronized_inference_ms') or '미측정'),
            '측정 FPS: ' + str(value.get('synthetic_fps') or '미측정'),
            '비교 요약: ' + str(value.get('worst_by_slice') or '미수행'),
            '실패 원인: ' + str(report.get('error') or value.get('error') or '기록 없음'),
            '등록된 비활성 후보: ' + str(len(receipts)) + '개 (최대 8개 조회)',
            '운영 엔진·HUD·차량 제어는 변경하지 않았습니다. 실차 검증·주행 승인은 별도입니다.',
          ])}


def n16_trial_status(root):
  """Bounded N16 trial status for 7000 ZIP; no model, CUDA, camera or HUD imports."""
  root = Path(root)
  value = record(root / 'cache/nexo-n16-trial-result.json', 65536)
  timeline = record(root / 'cache/nexo-n16-trial-memory.json', 1024 * 1024)
  state = str(value.get('state') or 'not_run')
  verified = state == 'passed' and value.get('gpu_inference_verified') is True
  worker = value.get('worker_result') if isinstance(value.get('worker_result'), dict) else {}
  diagnosis = value.get('diagnosis') if isinstance(value.get('diagnosis'), dict) else {}
  observer = value.get('hud_observer') if isinstance(value.get('hud_observer'), dict) else {}
  return {'ok': True, 'schema_version': 1, 'read_only': True,
          'source': 'recorded_offroad_n16_trial', 'trial': {
            'state': state, 'phase': value.get('phase'), 'error': str(value.get('error') or '')[:300],
            'model_sha256': value.get('model_sha256'), 'workspace_mib': value.get('workspace_mib'),
            'optimization_level': value.get('optimization_level'), 'split_builder': value.get('split_builder'),
            'gpu_inference_verified': verified, 'worker_phase': worker.get('phase'),
            'artifacts_deleted': value.get('artifacts_deleted'),
          },
          'memory': {'samples_taken': timeline.get('samples_taken'),
                     'minimum_memavailable_bytes': timeline.get('minimum_memavailable_bytes'),
                     'maximum_worker_rss_bytes': timeline.get('maximum_worker_rss_bytes'),
                     'cuda_memory_peak_verified': False,
                     'recent_samples': (timeline.get('samples') or [])[-5:]},
          'diagnosis': {'code': diagnosis.get('diagnosis_code') or 'unclassified_or_legacy',
                        'production_activation_allowed': False},
          'hud_observer': {'mode': 'offline_read_only_observer',
                           'available_output_names': ['lane_lines', 'road_edges', 'lead', 'lead_prob', 'plan'],
                           'traffic_lights_available_from_n16_contract': False,
                           'blockers': observer.get('blockers') or
                                       ['reference numerical parity, NEXO camera and HUD geometry unverified'],
                           'physical_hud_feed_enabled': False, 'vehicle_control_enabled': False,
                           'renderable_objects': []}}


def build_failure_diagnosis(details):
  # The current classifier is embedded in the SSH payload, so even a legacy
  # sidecar/older runtime can be explained without modifying installed files.
  classifier = globals().get('classify_failure')
  if classifier is None:
    try:
      from openpilot.tools.jetson.trt_diagnostics import classify_failure as classifier
    except ImportError:
      return details.get('failure_diagnosis')
  messages = []
  for key in ('warning_error_tail', 'log_tail'):
    rows = details.get(key)
    if isinstance(rows, list):
      messages.extend(rows[-2200:])
  return classifier(details.get('error'), messages)


def experiment_logs(root):
  """Bounded read-only build evidence. No CUDA imports or arbitrary file walks."""
  report = record(root / 'cache/nexo-engine-experiments.json', 1024 * 1024)
  if not report:
    return {'ok': True, 'experiments': {'available': False, 'reason': 'not_run'}}
  stage = Path(str(report.get('stage_directory') or '')).resolve()
  if stage.parent != (root / 'cache').resolve() or not stage.name.startswith('nexo-experiment-'):
    return {'ok': False, 'error': 'Invalid experimental stage path; files not read'}
  logs = []
  candidate = report.get('candidate') if isinstance(report.get('candidate'), dict) else report
  if report.get('state') == 'running':
    candidate = record(stage / 'result.json', 1024 * 1024) or candidate
    if type(report.get('supervisor_pid')) is int:
      try:
        pid = report['supervisor_pid']
        same = (Path('/proc/sys/kernel/random/boot_id').read_text().strip() == report.get('boot_id')
                and Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19] == report.get('supervisor_start'))
      except (FileNotFoundError, ProcessLookupError, IndexError):
        same = False
      if not same:
        report.update(state='interrupted', error='시험 감독 프로세스 종료 또는 젯슨 재부팅')
  rows = list(candidate.get('trials') or [])
  current = candidate.get('current_trial')
  if report.get('state') in ('running', 'failed') and current and not any(row.get('name') == current for row in rows if isinstance(row, dict)):
    rows.append({'name': current, 'state': report['state']})
  selected = [row for row in rows[:12] if isinstance(row, dict) and row.get('state') in ('failed', 'running')]
  selected = sorted(selected, key=lambda row: row.get('name') not in ('native_fp16', 'native_strongly_typed', 'fp32_compute'))[:4]
  for row in selected:
    name = row.get('name')
    if not isinstance(name, str) or not re.fullmatch('[a-z0-9_]{1,40}', name):
      continue
    path = stage / name / 'experiment.build.json'
    if path.is_symlink() or path.parent.resolve().parent != stage:
      continue
    if not path.is_file():
      logs.append({'trial': name, 'state': 'awaiting_build_record'})
      continue
    try:
      detail = json.loads(_safe_text(path, 6 * 1024**2))
      verbose_tail = ''
      try:
        with (path.parent / 'experiment.trt.log').open('rb') as stream:
          stream.seek(0, 2)
          stream.seek(max(0, stream.tell() - 2048))
          verbose_tail = stream.read(2048).decode('utf-8', errors='replace')
      except OSError:
        pass
      evidence = {**detail, 'log_tail': [*(detail.get('log_tail') or []), {'message': verbose_tail}]}
      logs.append({'trial': name, 'state': detail.get('state'), 'workspace_bytes': detail.get('workspace_bytes'),
                   'available_bytes': detail.get('available_bytes'), 'failure_diagnosis': build_failure_diagnosis(evidence),
                   'verbose_log_path': detail.get('verbose_log_path'), 'full_log_truncated': detail.get('full_log_truncated'),
                   'verbose_file_tail': verbose_tail, 'build_memory': record(stage / name / 'build-memory.json'),
                   'warning_error_tail': [str(v.get('message', ''))[:800] for v in detail.get('warning_error_tail', [])[-6:]
                                          if isinstance(v, dict)],
                   'verbose_tail': [str(v.get('message', ''))[:400] for v in detail.get('log_tail', [])[-12:]
                                    if isinstance(v, dict)], 'bounded_excerpt': True})
    except (OSError, ValueError, TypeError, AttributeError) as error:
      logs.append({'trial': name, 'error': type(error).__name__})
  # A later timeout can leave successful trials only in the child snapshot.
  # Report completed measurements even when the whole matrix did not finish.
  summary_keys = ('name', 'state', 'error', 'measured_runs', 'synchronized_inference_ms', 'synthetic_fps',
                  'reference_scope', 'reference_unavailable', 'lead_comparison', 'plan_comparison', 'cleanup_error',
                  'validated_outputs', 'failed_phase', 'memory_before', 'memory_after_build', 'memory_after',
                  'build_memory', 'workspace_bytes', 'optimization_level', 'tactic_sources', 'failure_diagnosis')
  trials = [{key: row[key] for key in summary_keys if key in row} for row in rows[:12] if isinstance(row, dict)]
  response = {'ok': True, 'experiments': {'available': True, 'state': report.get('state'), 'current_trial': current,
          'stage_directory': str(stage),
          'native_gpu_verified': candidate.get('native_gpu_verified') is True, 'production_cache_published': False,
          'trials': trials, 'error': report.get('error') or candidate.get('error'),
          'model_sha256': candidate.get('model_sha256'),
          'failed_build_excerpts': logs, 'build_artifacts_preserved_in_stage': True,
          'note': '최대 4개 실패의 제한된 로그입니다. 실험 폴더의 VERBOSE 로그는 각 32MiB까지 보존하고 초과 시 잘림을 표시합니다. plan은 활성화하지 않습니다.'}}
  while len(json.dumps(response, ensure_ascii=False).encode('utf-8')) > 60000 and logs:
    logs.pop()
    response['experiments']['excerpt_budget_truncated'] = True
  return response


def hud_receipt(now=None):
  """Read only receipt/size metadata; never expose JPEG or snapshot contents."""
  clock_now = now is None
  result = {}
  for path in (Path('/dev/shm/nexo-jetson/hud.json'), Path('/dev/shm/carrot-jetlink-hud.packet')):
    try:
      with path.open('rb') as source:
        raw = source.read(512 * 1024 + 1033)
      if len(raw) >= 512 * 1024 + 1033:
        continue
      if path.suffix == '.packet':
        received, = struct.unpack_from('<d', raw)
        value = json.loads(raw[8:])
      else:
        value = json.loads(raw)
        received = value['updated']
      checked = time.monotonic() if clock_now else now
      if value.get('version') != 1 or not 0 <= checked - received < .5:
        continue
      result['hud_rx_age_s'] = checked - received
      for name in ('road', 'wide', 'driver'):
        frame = value.get('cameras', {}).get(name) or {}
        if not frame or not 0 <= value['sent'] - frame['time'] < .3:
          continue
        frame_age = checked - received + value['sent'] - frame['time']
        if frame_age >= .5 or frame.get('width') != 384 or not 2 <= frame['height'] <= 384 or len(frame['jpeg']) > 44 * 1024:
          continue
        # A JPEG envelope can be observed without Pillow, but is never
        # equivalent to a successful image decode or usable camera pixels.
        try:
          encoded = base64.b64decode(frame['jpeg'], validate=True)
        except (ValueError, TypeError, base64.binascii.Error):
          result['decode_error'] = '수신 JPEG Base64 형식 오류'
          continue
        if not encoded.startswith(b'\xff\xd8\xff') or not encoded.endswith(b'\xff\xd9'):
          result['decode_error'] = '수신 JPEG 시작/종료 표식 오류'
          continue
        receipt = dict(result, camera_rx_age_s=frame_age, camera_rx_stream=name, image_envelope_ok=True)
        try:
          from PIL import Image
        except ImportError:
          return dict(receipt, decoded=False, decode_error='Pillow 없음 · JPEG 수신만 확인, 디코딩 미검증')
        try:
          with Image.open(io.BytesIO(encoded)) as image:
            if image.format != 'JPEG' or image.size != (384, frame['height']):
              continue
            image.load()
        except (ValueError, OSError):
          result['decode_error'] = '수신 JPEG 디코딩 실패'
          continue
        return dict(receipt, decoded=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, struct.error):
      continue
  return result


def plan_key_matches(stem, model_sha, version, device):
  if not isinstance(model_sha, str) or not re.fullmatch('[0-9a-f]{64}', model_sha):
    return False
  prefix = model_sha[:16] + '.trt'
  if not stem.startswith(prefix):
    return False
  if version is None and device is None:
    # Older sidecars only identified the model. This is file presence evidence.
    return stem == prefix or re.fullmatch(r'[0-9][A-Za-z0-9._-]*\.[A-Za-z0-9_-]+', stem[len(prefix):]) is not None
  if not (isinstance(version, str) and version and isinstance(device, str) and device):
    return False
  tag = re.sub(r'[^A-Za-z0-9._-]', '_', version + '.' + device)
  return stem == prefix + tag


def installed_model(root, *, hash_other_plans=True):
  current = (root / 'current').resolve()
  manifest = record(current / 'NEXO_RUNTIME.json')
  model = current / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
  sha = None
  try:
    digest = hashlib.sha256()
    with model.open('rb') as source:
      while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    sha = digest.hexdigest()
  except OSError:
    pass
  spec = manifest.get('model') or {}
  engines, inventory, build_errors = [], [], []
  # Read-only, bounded explicit diagnostics; no hash walks in the HUD frame path.
  directory = root / 'cache/engines'
  metadata_files = sorted(directory.glob('*.json'),
                          key=lambda p: (not p.name.startswith(sha[:16]) if sha else True, p.name))[:32]
  for meta in metadata_files:
    if meta.name.endswith('.build.json'):
      try:
        with meta.open('rb') as source:
          details = json.loads(source.read(8 * 1024**2 + 1))
      except (OSError, ValueError):
        details = {}
      if isinstance(details, dict) and details.get('state') == 'failed':
        build_errors.append({'file': meta.name, 'state': 'failed',
                             'failure_diagnosis': build_failure_diagnosis(details),
                             'error': str(details.get('error', ''))[:1000],
                             'trt_version': details.get('trt_version'), 'device': details.get('device'),
                             'workspace_bytes': details.get('workspace_bytes'),
                             'available_bytes': details.get('available_bytes'),
                             'fp16': details.get('fp16'),
                             'optimization_level': details.get('optimization_level'),
                             'failed_phase': details.get('failed_phase'),
                             'elapsed_seconds': details.get('elapsed_seconds'),
                             'warning_error_tail': [str(item)[:600] for item in details.get('warning_error_tail', [])[-8:]]})
      continue
    details = record(meta)
    contract = details.get('spec') if isinstance(details.get('spec'), dict) else {}
    model_sha = contract.get('sha256')
    plan = meta.with_suffix('.plan')
    if not plan.is_file():
      continue
    # Carrot keys a plan by the model digest prefix and the backend tag.
    # Legacy sidecars may only carry the model spec; keep that evidence separate.
    version, device = details.get('trt_version'), details.get('device')
    key_matches = plan_key_matches(meta.stem, model_sha, version, device)
    current_key = bool(sha and model_sha == sha and key_matches)
    if current_key:
      engines.append(meta.stem)
    if len(inventory) >= 4:
      continue
    item = {'file': plan.name, 'model_sha256': model_sha,
            'matches_installed_onnx': bool(sha and model_sha == sha),
            'cache_key_matches_metadata': key_matches, 'trt_version': version, 'device': device,
            'plan_sha256': None, 'loadability_verified': False}
    try:
      item['plan_bytes'] = plan.stat().st_size
      if not hash_other_plans and not current_key:
        item['hash_status'] = 'skipped_unrelated_plan'
      elif item['plan_bytes'] <= 2 * 1024**3:
        digest = hashlib.sha256()
        with plan.open('rb') as source:
          while chunk := source.read(1024 * 1024):
            digest.update(chunk)
        item['plan_sha256'] = digest.hexdigest()
      else:
        item['hash_status'] = 'file_exceeds_2_gib_diagnostic_budget'
    except OSError as error:
      item['hash_error'] = str(error)[:200]
    inventory.append(item)
  # A fingerprint is provenance evidence, not proof that TensorRT can build.
  builder = current / 'carrot-server/third_party/jetlink/jetlink/server/backends/trt/build.py'
  builder_fingerprint = {'available': False, 'sha256': None, 'source': 'installed_runtime'}
  try:
    if builder.is_file() and not builder.is_symlink() and builder.stat().st_size < 1024 * 1024:
      builder_fingerprint.update(available=True, sha256=hashlib.sha256(builder.read_bytes()).hexdigest())
  except OSError:
    pass
  try:
    model_bytes = model.stat().st_size if model.is_file() else None
  except OSError:
    model_bytes = None
  versions = run(['dpkg-query', '-W', '-f=${Package} ${Version}\n', 'nvidia-l4t-core', 'libnvinfer10'])
  matrix = record(root / 'cache/nexo-engine-experiments.json', 1024 * 1024)
  compact = {key: matrix[key] for key in ('state', 'error', 'model_sha256', 'native_gpu_verified', 'production_cache_published',
                                          'current_trial') if key in matrix}
  if matrix:
    compact['trials'] = [{k: row[k] for k in ('name', 'state', 'measured_runs', 'synthetic_fps', 'synchronized_inference_ms') if k in row}
                         for row in (matrix.get('trials') or [])[:12] if isinstance(row, dict)]
  return {'onnx_sha256': sha, 'onnx_bytes': model_bytes, 'package_contract': spec,
          'matching_engine_files': engines, 'builder_fingerprint': builder_fingerprint,
          'installed_packages': versions['output'], 'package_runtime': manifest.get('runtime'),
          'engine_inventory': inventory, 'engine_build_errors': build_errors[-4:],
          'engine_preparation': record(root / 'cache/nexo-engine-preparation.json'),
          'engine_experiments': compact,
          'identity_note': 'Compare ONNX SHA to metadata model_sha256; plan_sha256 fingerprints different compiled bytes, not the ONNX.'}


def usb_role_policy():
  # The installed FUSB301 driver owns role negotiation; diagnostics never write it.
  base = Path('/sys/bus/i2c/devices/1-0025/fusb301')
  value = {}
  for name in ('fmode', 'fsw_trysnk'):
    try:
      text = (base / name).read_text().strip()
      if re.fullmatch(r'(?:SRC|SRC\+ACC|SNK|SNK\+ACC|DRP|DRP\+ACC)\(\d+\)|[01]', text):
        value[name] = text
    except OSError:
      pass
  return value


def run(args, timeout=3):
  result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
  return {'code': result.returncode, 'output': (result.stdout + result.stderr)[-16000:]}


def record(path, limit=65536):
  try:
    with Path(path).open('rb') as source:
      raw = source.read(limit + 1)
      value = json.loads(raw) if len(raw) <= limit else {}
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}



def read_hud_trace(path=Path('/dev/shm/nexo-jetson/hud-pipeline-trace.json')):
  """Read bounded HUD pipeline metadata, never raw camera or JPEG images."""
  path = Path(path)
  try:
    if path.is_symlink() or path.stat().st_size > 65536:
      return {'ok': False, 'error': 'HUD 추적 파일 형식 또는 크기를 확인할 수 없습니다.'}
    with path.open('rb') as source:
      raw = source.read(65537)
    if len(raw) > 65536:
      return {'ok': False, 'error': 'HUD 추적 파일 크기 초과'}
    value = json.loads(raw)
    if not isinstance(value, dict):
      return {'ok': False, 'error': 'HUD 추적 데이터가 JSON 객체가 아닙니다.'}
    updated = value.get('updated')
    now = time.monotonic()
    age = round(now - updated, 3) if type(updated) in (float, int) and 0 <= now - updated < 86400 else None
    return {'ok': True, 'hud_pipeline_trace': value, 'trace_age_s': age,
            'note': '읽기 전용 젯슨 HUD 파이프라인 관측입니다. 화면의 물리적 점등은 검증하지 않습니다.'}
  except (OSError, ValueError, UnicodeError) as error:
    return {'ok': False, 'error': f'HUD 추적 파일 확인 불가: {type(error).__name__}'}


# Bounded Jetson AI/HUD performance investigation. This helper is streamed to
# Jetson via SSH stdin, so it works even when the protected HUD image is old.
# No process signals, nvpmodel writes, CUDA calls, GC forcing, or network changes.
def _safe_text(path, limit=8192):
  try:
    with Path(path).open('rb') as stream:
      return stream.read(limit + 1).decode('utf-8', errors='replace')[:limit]
  except (OSError, ValueError):
    return ''


def _safe_number(value):
  try:
    number = float(value)
    return number if 0 <= number < 1e15 else None
  except (TypeError, ValueError, OverflowError):
    return None


def _cpu_counters():
  counters = {}
  for line in _safe_text('/proc/stat', 12288).splitlines():
    items = line.split()
    if not items or not re.fullmatch(r'cpu(?:\d+)?', items[0]):
      continue
    try:
      ticks = [int(v) for v in items[1:9]]
      counters[items[0]] = (sum(ticks), ticks[3] + ticks[4])
    except (ValueError, IndexError):
      continue
  return counters


def _cpu_usage(before, after):
  result = {}
  for name, (total, idle) in after.items():
    old = before.get(name)
    if old and total > old[0] and idle >= old[1]:
      result[name] = round(max(0., min(100., 100 * (1 - (idle - old[1]) / (total - old[0])))), 1)
  return result


def _main_pids(units):
  result = {}
  for name in ('carrot-jetlink.service', 'carrot-jetlink-hud.service', 'nexo-jetlink-server.service',
               'nexo-jetson-hud.service', 'nexo-jetson-host.service'):
    if name not in units or units[name].get('ActiveState') != 'active':
      continue
    try:
      raw = run(['systemctl', 'show', name, '--value', '-p', 'MainPID'], timeout=1.5)
      number = raw['output'].strip()
      if raw['code'] == 0 and number.isdecimal() and 1 < int(number) <= 4194304:
        result[name] = int(number)
    except (OSError, subprocess.SubprocessError):
      continue  # An unresponsive unit must not discard all other diagnostics.
  return result


def _proc_memory(pid):
  fields = {'VmRSS': 'rss_kb', 'VmHWM': 'peak_rss_kb', 'VmSwap': 'swap_kb',
            'RssAnon': 'anonymous_kb', 'RssFile': 'file_kb', 'RssShmem': 'shared_kb',
            'Threads': 'threads', 'voluntary_ctxt_switches': 'voluntary_context_switches',
            'nonvoluntary_ctxt_switches': 'involuntary_context_switches'}
  result = {}
  for line in _safe_text(f'/proc/{pid}/status', 16384).splitlines():
    key, _, value = line.partition(':')
    if key in fields:
      digits = value.strip().split()
      if digits and digits[0].isdecimal():
        result[fields[key]] = int(digits[0])
  for line in _safe_text(f'/proc/{pid}/smaps_rollup', 8192).splitlines():
    key, _, value = line.partition(':')
    if key in ('Pss', 'Private_Dirty', 'SwapPss'):
      parts = value.strip().split()
      if parts and parts[0].isdecimal():
        result[{'Pss': 'pss_kb', 'Private_Dirty': 'private_dirty_kb',
                'SwapPss': 'swap_pss_kb'}[key]] = int(parts[0])
  return result


def _thread_counters(pids):
  rows = {}
  for role, pid in list(pids.items())[:5]:
    for path in sorted(Path(f'/proc/{pid}/task').glob('[0-9]*'))[:48]:
      tid = int(path.name)
      parts = _safe_text(path / 'schedstat', 128).split()
      if len(parts) >= 2 and all(value.isdecimal() for value in parts[:2]):
        rows[(role, tid)] = (int(parts[0]), int(parts[1]))
  return rows


def _thread_usage(before, after, elapsed):
  rows = []
  if not 0.05 <= elapsed <= 3:
    return rows
  for (role, tid), (cpu, wait) in after.items():
    old = before.get((role, tid))
    if old is None or cpu < old[0] or wait < old[1]:
      continue
    rows.append({'service': role, 'tid': tid, 'cpu_pct_one_core': round((cpu-old[0]) / 1e7 / elapsed, 2),
                 'runqueue_wait_ms': round((wait-old[1])/1e6, 3),
                 'cpu_time_ms': round((cpu-old[0])/1e6, 3), 'interval_s': round(elapsed, 3)})
  rows.sort(key=lambda x: (x['runqueue_wait_ms'], x['cpu_pct_one_core']), reverse=True)
  return rows[:12]


def _gpu_evidence():
  clocks = []
  for path in sorted(Path('/sys/class/devfreq').glob('*'))[:80]:
    name = path.name.lower()
    if not any(word in name for word in ('gpu', 'ga10b', 'gv11b', '17000000', '57000000')):
      continue
    metrics = {}
    for key, filename in (('clock_mhz','cur_freq'), ('min_clock_mhz','min_freq'), ('max_clock_mhz','max_freq')):
      value = _safe_number(_safe_text(path / filename, 128).strip())
      metrics[key] = round(value / 1e6, 2) if value is not None else None
    value = _safe_number(_safe_text(path / 'device/load', 128).strip())
    metrics['load_pct'] = round(value / 10, 1) if value is not None and value <= 1000 else None
    clocks.append({'device': name[:90], **metrics})
    if len(clocks) >= 4:
      break
  base = Path('/sys/kernel/debug/bpmp/debug/clk/emc')
  emc = _safe_number(_safe_text(base / 'rate', 128).strip())
  return {'gpu': clocks, 'emc_clock_mhz': round(emc / 1e6, 2) if emc is not None else None,
          'gpu_available': bool(clocks), 'note': 'GPU 부하·EMC는 드라이버/권한이 허용한 경우만 기록합니다.'}


def _power_mode():
  """Report current and declared modes, without changing Jetson power settings."""
  try:
    value = run(['/usr/sbin/nvpmodel', '-q'], timeout=1)
    match = re.search(r'NV Power Mode:\s*([^\r\n]+)\s*\n\s*(\d+)', value['output'])
    result = {'mode': match[1].strip()[:72], 'id': int(match[2]), 'available': True} if match else {'available': False}
  except (OSError, subprocess.SubprocessError):
    result = {'available': False}
  declared = []
  for path in (Path('/etc/nvpmodel.conf'), Path('/etc/nvpmodel/nvpmodel.conf')):
    if path.is_symlink():
      continue
    for ident, name in re.findall(r'<\s*POWER_MODEL\s+ID\s*=\s*(\d+)\s+NAME\s*=\s*([A-Za-z0-9_+.-]+)\s*>',
                                  _safe_text(path, 65536), flags=re.I):
      item = {'id': int(ident), 'name': name[:64]}
      if item not in declared:
        declared.append(item)
    if declared:
      break
  result.update(declared_modes=declared[:24],
                maxn_super_declared=any(v['name'].upper() == 'MAXN_SUPER' for v in declared) if declared else None,
                maxn_super_verified=False, automatic_power_change=False,
                note='펌웨어 및 보드 호환성 확인 전에는 MAXN_SUPER를 설정하지 않습니다.')
  return result


def _meminfo():
  allowed = ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree', 'Cached', 'SReclaimable')
  result = {}
  for line in _safe_text('/proc/meminfo', 8192).splitlines():
    key, _, value = line.partition(':')
    bits = value.strip().split()
    if key in allowed and bits and bits[0].isdecimal():
      result[key.lower() + '_kb'] = int(bits[0])
  return result


def _python_gc():
  # Produced in a separate observer thread in the Jetson HUD renderer only.
  path = Path('/dev/shm/nexo-jetson/hud-python-gc.json')
  if path.is_symlink():
    return {'available': False, 'reason': 'untrusted_gc_path'}
  value = record(path)
  stamp, pid = value.get('updated'), value.get('pid')
  now = time.monotonic()
  if (type(pid) is not int or not 1 < pid <= 4194304 or
      type(stamp) not in (int, float) or not 0 <= now-stamp < 15 or
      not Path(f'/proc/{pid}').is_dir()):
    return {'available': False, 'reason': 'hud_gc_observer_unavailable_or_stale'}
  return {'available': True, 'age_s': round(now-stamp, 2), 'pid': pid,
          'generations': (value.get('generations') or [])[:3],
          'recent_max_pause_ms': value.get('recent_max_pause_ms'),
          'gc_pause_total_ms': value.get('gc_pause_total_ms'),
          'gc_enabled': value.get('gc_enabled') if type(value.get('gc_enabled')) is bool else None,
          'sample_interval_s': value.get('sample_interval_s'),
          'monitoring_only': True}


def performance_sample(units, root, interval=.35):
  """Read-only on-demand CPU/GPU/memory/scheduler/GC + HUD evidence.

  Process/thread deltas use the SAME Jetson monotonic clock. No Linux
  device is modified. Missing samples are UNKNOWN, never fake zero.
  """
  interval = max(.2, min(.8, float(interval)))
  pids = _main_pids(units)
  gc = _python_gc()
  if gc.get('available') is True and gc['pid'] not in pids.values():
    pids['nexo-hud-renderer'] = gc['pid']
  cpu_before, thread_before, started = _cpu_counters(), _thread_counters(pids), time.monotonic()
  time.sleep(interval)
  elapsed = time.monotonic()-started
  cpu_after, thread_after = _cpu_counters(), _thread_counters(pids)
  processes = [{'service': service, 'pid': pid, **_proc_memory(pid)}
               for service, pid in pids.items()]
  hud_trace = read_hud_trace()
  trace = hud_trace.get('hud_pipeline_trace') or {} if hud_trace.get('ok') else {}
  hud = {key: trace.get(key) for key in (
      'input_mode', 'input_reason', 'input_age_s', 'encoded_age_s',
      'panel_write_age_s', 'panel_gap_count', 'panel_gap_max_s',
      'encoder_sent_count', 'encoder_expired_count', 'waiting_episodes',
      'waiting_max_s', 'panel_gap_threshold_s', 'gear_missing_count',
      'gear_missing_max_s', 'gear_scene_changes', 'clock_generation') if key in trace}
  panel = record(Path('/dev/shm/nexo-jetson/hud-status.json'))
  stamp = panel.get('updated')
  if type(stamp) in (int, float) and 0 <= time.monotonic() - stamp < 3:
    for key in ('render_ms', 'panel_send_ms', 'encoder_wait_ms'):
      value = panel.get(key)
      if type(value) in (int, float) and 0 <= value < 10000:
        hud[key] = value
  control = control_state(root)
  engine = control.get('engine', {})
  return {'ok': True, 'performance': {
    'schema_version': 1, 'read_only': True,
    'sample_monotonic': round(time.monotonic(), 3), 'interval_s': round(elapsed, 3),
    'cpu_busy_pct': _cpu_usage(cpu_before, cpu_after),
    'clocks': _gpu_evidence(), 'power_mode': _power_mode(),
    'memory': _meminfo(), 'processes': processes,
    'server_scheduler_threads': _thread_usage(thread_before, thread_after, elapsed),
    'scheduler_observation_available': bool(thread_after),
    'hud_python_gc': gc, 'hud_pipeline': hud, 'hud_trace_age_s': hud_trace.get('trace_age_s'),
    'engine': {key: engine[key] for key in ('state','detail') if key in engine},
    'limitations': [
      'A short CPU/scheduler sample cannot establish the cause of a specific historical frame stall.',
      'GPU load and clocks require readable Jetson driver interfaces.',
      'GC observations cover only the Jetson HUD Python renderer, not native TensorRT/CUDA pauses.',
      'Reported panel writes do not verify physical screen illumination.',
      'Jetson model readiness does not prove external driving inference without fresh comma modeld evidence.',
    ],
  }}



def read_performance_history(path=Path('/dev/shm/nexo-jetson/performance-history.json')):
  """Streamed read-only SSH helper; works even with an older Jetson image."""
  try:
    if path.is_symlink() or path.stat().st_size > 65536:
      raise ValueError('unsafe history')
    value = json.loads(_safe_text(path, 65536))
    if value.get('schema_version') != 1 or value.get('read_only') is not True:
      raise ValueError('unknown history')
    stamp = value.get('updated')
    delta = time.monotonic() - stamp if type(stamp) in (int, float) else None
    fields = ('jetson_mono_s', 'jetson_unix_s', 'cpu_busy_pct', 'gpu_clock_mhz',
              'gpu_load_pct', 'power_mode', 'memavailable_kb', 'max_runqueue_wait_ms',
              'panel_gap_count', 'panel_gap_delta', 'panel_gap_max_s',
              'encoder_expired_count', 'hud_trace_age_s', 'engine_state',
              'hud_gap_observed_in_interval')
    samples = [{k: row[k] for k in fields if k in row}
               for row in (value.get('samples') or [])[-48:] if isinstance(row, dict)]
    corr = value.get('correlation') or {}
    return {'ok': True, 'history': {'available': True,
            'fresh': delta is not None and 0 <= delta < 20,
            'age_s': round(delta, 2) if delta is not None else None,
            'read_only': True, 'samples': samples,
            'correlation': {k: corr[k] for k in ('gap_samples', 'gap_with_high_cpu',
              'gap_with_scheduler_wait', 'causation_proven', 'note') if k in corr},
            'note': '젯슨 로컬 시계의 기록이며 콤마4 시간과 직접 비교할 수 없습니다.'}}
  except (OSError, ValueError, TypeError):
    return {'ok': True, 'history': {'available': False, 'reason': 'observer_missing_or_invalid'}}


def services():
  found = {}
  for unit in SERVICES:
    result = run(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
                  '-p', 'UnitFileState', '-p', 'WorkingDirectory', '-p', 'ExecStart'])
    values = dict(line.split('=', 1) for line in result['output'].splitlines() if '=' in line)
    if values.get('LoadState') == 'loaded':
      # ExecStart is used internally to find the runtime, not exposed as settings/secrets.
      found[unit] = values
  return found


def root_for(units):
  native = units.get('nexo-jetlink-server.service', {})
  if native.get('ActiveState') == 'active' and native.get('WorkingDirectory'):
    return Path(native['WorkingDirectory']).resolve()
  directory = units.get('carrot-jetlink.service', {}).get('WorkingDirectory', '')
  if directory:
    root = Path(directory).resolve()
    # Runtime layout from Windows image; versioned runtime sits below current.
    for parent in (root, *root.parents):
      if (parent / 'cache').is_dir() and (parent / 'updates').is_dir():
        return parent
  return Path('/opt/carrot-jetlink')


def control_state(root):
  result = {}
  path = root / 'control.sock'
  if not path.exists():
    return result
  try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
      sock.settimeout(.5)
      sock.connect(str(path))
      pending = b''
      deadline = time.monotonic() + 1
      while time.monotonic() < deadline and len(pending) < 65536:
        raw = sock.recv(8192)
        if not raw:
          break
        pending += raw
        while b'\n' in pending:
          line, pending = pending.split(b'\n', 1)
          event = json.loads(line)
          if event.get('event') in ('server', 'link', 'engine'):
            result[event['event']] = {k: v for k, v in event.items() if k not in ('event', 't')}
        if all(key in result for key in ('server', 'link', 'engine')):
          break
  except (OSError, ValueError):
    pass
  return result


def usb_devices():
  for device in Path('/sys/bus/usb/devices').glob('*'):
    try:
      if (device / 'idVendor').read_text().strip() != '1209' or (device / 'idProduct').read_text().strip() != '0001':
        continue
      speed = float((device / 'speed').read_text())
      return {'usb_connected': True, 'usb3': speed >= 5000, 'usb_speed': f'{speed:g} Mbps'}
    except (OSError, ValueError):
      pass
  return {'usb_connected': False, 'usb3': None, 'usb_speed': '미연결'}


def latest_carrot(root, units):
  # Only inspect metadata. Download/stage/activation functions are never called.
  with urllib.request.urlopen(MANIFEST_URL, timeout=3) as response:
    if response.url != MANIFEST_URL:
      raise ValueError('unexpected manifest redirect')
    raw = response.read(256 * 1024 + 1)
  if len(raw) > 256 * 1024:
    raise ValueError('manifest exceeded size limit')
  manifest = json.loads(raw)
  source = manifest.get('source_commit')
  if manifest.get('format') != 1 or not isinstance(source, str) or not re.fullmatch('[0-9a-f]{40}', source):
    raise ValueError('invalid update manifest identity')
  verified = False
  verification_error = ''
  try:
    import base64
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa
    cwd = Path(units.get('carrot-jetlink.service', {}).get('WorkingDirectory', str(root)))
    key_path = next(p for p in (cwd / 'tools/jetlink/release-signing-public.pem',
                                root / 'current/tools/jetlink/release-signing-public.pem') if p.is_file())
    key = ECC.import_key(key_path.read_text())
    payload = json.dumps({k: v for k, v in manifest.items() if k != 'signature'}, sort_keys=True, separators=(',', ':')).encode()
    eddsa.new(key, 'rfc8032').verify(payload, base64.b64decode(manifest['signature'], validate=True))
    verified = True
  except (ImportError, OSError, ValueError, KeyError, StopIteration) as error:
    verification_error = f'{type(error).__name__}: {error}'[:240]
  installed = (root / 'current').resolve().name
  return {'source_commit': source, 'signature_verified': verified, 'verification_error': verification_error,
          'available': source != installed if verified and re.fullmatch('[0-9a-f]{40}', installed) else None,
          'applied': False}


def installed_source_commit(root):
  """Normalize the protected nexo-<SHA> release and its SOURCE_COMMIT marker."""
  try:
    value = (root / 'current/SOURCE_COMMIT').read_text(encoding='utf-8').strip()
    if re.fullmatch('[0-9a-f]{40}', value):
      return value
  except OSError:
    pass
  name = (root / 'current').resolve().name
  if name.startswith('nexo-'):
    name = name[5:]
  return name if re.fullmatch('[0-9a-f]{40}', name) else ''


def collect(units, root):
  now = time.monotonic()
  files = {}
  for name, path in (
    ('health', '/dev/shm/carrot-jetlink-health.json'),
    ('network', '/dev/shm/carrot-jetlink-network-status.json'),
    ('display', '/dev/shm/nexo-jetson/status.json'),
    ('video', '/dev/shm/nexo-jetson/video-input.json'),
    ('yolo', '/dev/shm/nexo-jetson/yolo.json'),
    ('direct', '/tmp/nexo-yolo-direct-status.json'),
  ):
    value = record(path)
    stamp = value.get('updated')
    if name == 'direct':
      # Legacy worker stores wall time on Jetson. Compare only with that host's clock.
      stamp = value.get('updated_ts')
      current = time.time()  # noqa: TID251
    else:
      current = now
    if isinstance(stamp, (int, float)) and 0 <= current - stamp < 5:
      files[name] = value
  control = control_state(root)
  frame = files.get('direct', {})
  if frame:
    frame_stamp = frame.get('last_frame_ts')
    yolo_stamp = frame.get('last_yolo_ts')
    frame_recent = isinstance(frame_stamp, (int, float)) and 0 <= time.time() - frame_stamp < 3  # noqa: TID251
    yolo_recent = isinstance(yolo_stamp, (int, float)) and 0 <= time.time() - yolo_stamp < 3  # noqa: TID251
  else:
    frame_recent = None
    yolo_recent = True if 'yolo' in files else None
  engine = control.get('engine', {})
  link = control.get('link', {})
  summary = {'magic': 'NEXO_JETSON_STATUS', 'host': socket.gethostname(),
             'service_active': (units['carrot-jetlink.service'].get('ActiveState') == 'active'
                                if 'carrot-jetlink.service' in units else None),
             'comma_connected': link.get('state') == 'connected' if link else None,
             'camera_frame_seen': frame_recent, 'model_active': yolo_recent,
             'model_ready': engine.get('state') == 'ready' if engine else None,
             'last_error': engine.get('detail', '') if engine.get('state') == 'failed' else '',
             'pipeline': 'SSH 관리 · ' + ', '.join(units), **usb_devices(), **hud_receipt()}
  version = installed_source_commit(root)
  if version:
    summary['jetson_version'] = version
  health = files.get('health', {})
  if health:
    summary['host_telemetry'] = {'carrot_health': dict(health, age_s=now - health['updated'])}
  return {'summary': summary, 'services': {unit: {k: v for k, v in info.items() if k in ('ActiveState', 'SubState', 'UnitFileState')}
                       for unit, info in units.items()},
          'runtime': str(root), 'control': control, 'telemetry': files,
          'model_cache': record(root / 'cache/last-loaded.json'), 'usb_role_policy': usb_role_policy(),
          'update': record(root / 'updates/status.json'), 'sleep': '수동 확인 필요 · 전원 정책 변경 없음'}


def inference_readiness(root):
  """Passive preflight only. Never install, rebuild, switch USB, or enable driving AI."""
  install = record(root / 'updates/nexo-install-status.json')
  ncm_config = record(root / 'updates/nexo-ncm.json')
  ncm_live = record('/run/nexo-ncm-status.json')
  model = installed_model(root, hash_other_plans=False)
  remembered = record(root / 'cache/last-loaded.json')
  sha = model.get('onnx_sha256')
  package = model.get('package_contract') or {}
  old_sha = remembered.get('sha256')
  plan_files = model.get('matching_engine_files') or []
  display_only = install.get('display_only') is True and ncm_config.get('display_only') is True
  stamp = ncm_live.get('updated')
  delta = time.monotonic() - stamp if type(stamp) in (int, float) else None
  ncm_ready = ncm_live.get('state') == 'ready' and delta is not None and 0 <= delta < 30
  try:
    free_bytes = shutil.disk_usage(root).free
  except OSError:
    free_bytes = None
  blockers = []
  if display_only or install.get('state') != 'installed':
    blockers.append('HUD_ONLY_RUNTIME' if display_only else 'AI_INSTALL_NOT_VERIFIED')
  if not sha or sha != package.get('sha256') or sha != ncm_config.get('model_sha256'):
    blockers.append('MODEL_CONTRACT_UNVERIFIED')
  if not plan_files:
    blockers.append('CURRENT_TRT_ENGINE_NOT_BUILT')
  if not ncm_ready:
    blockers.append('USB_NCM_NOT_FRESH_READY')
  if free_bytes is None or free_bytes < 4 * 1024**3:
    blockers.append('FREE_SPACE_BELOW_OR_UNKNOWN')
  if (root / 'updates/nexo-install-transaction.json').exists():
    blockers.append('INTERRUPTED_INSTALL_RECOVERY_REQUIRED')
  preparation = model.get('engine_preparation') or {}
  experiments = model.get('engine_experiments') or {}
  native_verified = (bool(sha) and experiments.get('model_sha256') == sha
                     and experiments.get('state') == 'experiment_complete' and experiments.get('native_gpu_verified') is True)
  preparation_verified = (preparation.get('state') == 'engine_ready' and preparation.get('synthetic_inference') is True
                          and preparation.get('model_sha256') == sha and bool(sha)
                          and any(item.get('cache_key_matches_metadata') is True and item.get('matches_installed_onnx') is True
                                  and item.get('file') == preparation.get('engine_name')
                                  and item.get('plan_sha256') == preparation.get('engine_sha256')
                                  and bool(item.get('plan_sha256')) for item in model.get('engine_inventory', [])))
  configured = ncm_config.get('interface')
  observed = ncm_live.get('interface') if ncm_ready else None
  renamed = bool(configured and observed and configured != observed)
  return {'ok': True, 'inference_readiness': {
    'schema_version': 1, 'read_only': True, 'activation_performed': False,
    'engine_build_performed': False, 'vehicle_inference_verified': False,
    'engine_build_verified': preparation_verified, 'synthetic_inference_verified': preparation_verified,
    'install_state': str(install.get('state') or 'unknown')[:80],
    'display_only_installed': display_only,
    'installed_sha256': sha, 'package_model_matches': bool(sha and sha == package.get('sha256')),
    'model_file_ready': bool(sha and sha == package.get('sha256') and sha == ncm_config.get('model_sha256')),
    'ncm_model_matches': bool(sha and sha == ncm_config.get('model_sha256')),
    'last_loaded_sha256': old_sha if isinstance(old_sha, str) and re.fullmatch('[0-9a-f]{64}', old_sha) else None,
    'old_cache_expected_in_hud_only': bool(display_only and old_sha and old_sha != sha),
    'matching_engine_files': list(plan_files)[:8], 'engine_file_present': bool(plan_files),
    'engine_inventory': model.get('engine_inventory', []),
    'engine_build_errors': model.get('engine_build_errors', []),
    'onnx_bytes': model.get('onnx_bytes'),
    'package_runtime': model.get('package_runtime'),
    'installed_packages': model.get('installed_packages'),
    'builder_fingerprint': model.get('builder_fingerprint'),
    'engine_preparation': model.get('engine_preparation', {}),
    'engine_experiments': experiments,
    'native_gpu_experiment_verified': native_verified,
    'ncm_fresh_ready': ncm_ready, 'configured_ncm_interface': configured,
    'observed_ncm_interface': observed,
    'ncm_interface_auto_recovered': renamed and ncm_ready,
    'free_bytes': free_bytes, 'min_free_bytes': 4 * 1024**3,
    'blockers': blockers,
    'note': ('HUD 전용 설치는 기존 TensorRT 캐시를 의도적으로 보존합니다. '
             'AI 설치는 Offroad에서 별도로 검증해야 하며 이 진단은 HUD, 모델, 차량 제어를 변경하지 않습니다.')}}


def handle(action):
  if action not in ACTIONS and action not in ('start_engine_experiment', 'comma_ssh_preflight', 'comma_ssh_host_preview', 'comma_ssh_host_enroll'):
    raise ValueError('unsupported management action')
  if not Path('/etc/nv_tegra_release').is_file():
    raise ValueError('SSH target is not a Jetson; operation refused')
  if action in ('start_engine_experiment', 'comma_ssh_preflight', 'comma_ssh_host_preview', 'comma_ssh_host_enroll'):
    expected_confirmation = '--confirmed-readonly' if action in ('comma_ssh_preflight', 'comma_ssh_host_preview') else '--confirmed-offroad'
    if len(sys.argv) < 4 or sys.argv[2] != expected_confirmation or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', sys.argv[3]):
      raise ValueError('명시적인 Offroad 시험 시작과 기존 콤마 SSH 계정이 필요합니다.')
    root = Path('/opt/carrot-jetlink')
    preflight = action in ('comma_ssh_preflight', 'comma_ssh_host_preview', 'comma_ssh_host_enroll')
    script = root / 'current/openpilot/tools/jetson' / ('ssh_preflight.py' if preflight else 'prepare_engine.py')
    if not script.is_file():
      return {'ok': False, 'error': '젯슨 HUD 패키지 업데이트 후 SSH 사전검사를 실행하세요.', 'code': 'RUNTIME_UPDATE_REQUIRED'}
    command = [str(root / 'venv/bin/python'), str(script)]
    if preflight:
      command += ['--target', sys.argv[3] + '@192.168.100.127']
      if action == 'comma_ssh_host_preview':
        command.append('--host-key-preview')
      elif action == 'comma_ssh_host_enroll':
        if len(sys.argv) != 5 or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', sys.argv[4]):
          raise ValueError('콤마4에서 별도로 확인한 호스트 공개키 SHA256 지문이 필요합니다.')
        command += ['--host-key-enroll', sys.argv[4]]
    else:
      command += ['--experiment', '--detach', '--samples', '20', '--comma-ssh', sys.argv[3] + '@192.168.100.127']
    extras = [] if action == 'comma_ssh_host_enroll' else list(sys.argv[4:])
    if action == 'comma_ssh_host_preview' and extras:
      raise ValueError('호스트키 미리보기에는 추가 인자가 허용되지 않습니다.')
    if extras and extras[0] != '--identity-file':
      name = extras.pop(0)
      if preflight or not re.fullmatch('[a-z][a-z0-9_]{0,39}', name):
        raise ValueError('잘못된 시험 이름')
      command += ['--trial', name]
    if extras:
      if len(extras) != 2 or extras[0] != '--identity-file' or not re.fullmatch('/[A-Za-z0-9_./@:+-]{1,239}', extras[1]) or '..' in Path(extras[1]).parts:
        raise ValueError('기존 개인키의 절대 경로만 지정하세요.')
      command += extras
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=25 if action == 'comma_ssh_host_enroll' else 15)
    try:
      value = json.loads(result.stdout)
    except ValueError:
      return {'ok': False, 'error': '젯슨 사전검사 응답 형식 오류', 'code': 'REMOTE_RESPONSE_INVALID'}
    if result.returncode:
      detail = value.get('ssh_preflight') or (value if preflight else {})
      return {'ok': False, 'error': value.get('error') or detail.get('recommendation') or '시험 시작 실패',
              'ssh_preflight': detail, 'code': detail.get('code')}
    return ({'ok': True, 'ssh_preflight': value} if action == 'comma_ssh_preflight' else
            {'ok': True, 'host_key': value} if preflight else {'ok': True, 'experiment': value})
  if action == 'n16_trial_status':
    return n16_trial_status(Path('/opt/carrot-jetlink'))
  if action == 'ssh_diagnostics':
    root = Path('/opt/carrot-jetlink')
    reverse = record(root / 'cache/nexo-comma-ssh-preflight.json')
    age = time.monotonic() - reverse.get('checked_monotonic', -1e9)
    try:
      fresh = reverse.get('boot_id') == Path('/proc/sys/kernel/random/boot_id').read_text().strip() and 0 <= age < 60
    except OSError:
      fresh = False
    return {'ok': True, 'jetson_to_comma': reverse, 'reverse_result_fresh': fresh,
            'reverse_age_s': round(age, 2) if age >= 0 and reverse else None,
            'note': '키 내용·비밀번호는 수집하지 않습니다. 역방향은 별도 인증이며 이전 결과와 현재 검증을 구분합니다.'}
  if action == 'performance_history':
    # The local observer already sampled the host. Do not spawn systemctl for
    # a management-screen poll of this bounded, read-only cached record.
    return read_performance_history()
  units = services()
  if not units and action not in ('model', 'video_test', 'hud_trace', 'performance', 'performance_history',
                                 'inference_readiness', 'engine_experiments', 'engine_candidates'):
    raise ValueError('supported Jetson service not installed')
  root = root_for(units)
  if action == 'engine_experiments':
    return experiment_logs(root)
  if action == 'engine_candidates':
    return candidate_logs(root)
  if action == 'hud_trace':
    return read_hud_trace()
  if action == 'performance':
    return performance_sample(units, root)
  if action == 'inference_readiness':
    return inference_readiness(root)
  if action in ('restart', 'reconnect'):
    if '--confirmed-offroad' not in sys.argv:
      raise ValueError('parked confirmation required')
    # Restart the running owners; preserve enablement, cache and host configuration.
    selected = [unit for unit, info in units.items() if info.get('ActiveState') == 'active']
    if not selected:
      selected = [next(iter(units))]
    result = run(['sudo', '-n', 'systemctl', 'restart', *selected], timeout=8)
    if result['code'] != 0:
      raise RuntimeError(result['output'] or 'service restart failed (sudo permission required)')
    return {'ok': True, 'restarted': selected, 'status': collect(services(), root)}
  if action == 'logs':
    command = ['journalctl', '--no-pager', '-n', '100', '-o', 'short-iso',
               *[arg for unit in units for arg in ('-u', unit)]]
    result = run(command)
    # Some protected images deny journal access to the jetlink user.
    # Retry READ ONLY through passwordless sudo without a prompt.
    if result['code'] != 0:
      elevated = run(['sudo', '-n', *command], timeout=4)
      if elevated['code'] == 0:
        result = elevated
    lines = result['output'].splitlines()
    sanitized = ['[민감 정보 포함 행 숨김]' if re.search(r'password|passwd|psk|secret|token|authorization', line, re.I) else line
                 for line in lines]
    return {'ok': result['code'] == 0, 'logs': '\n'.join(sanitized)}
  if action == 'check_update':
    # Read-only comparison: stock Carrot's signed updater is never run here.
    value = {'installed': str((root / 'current').resolve()), 'staged': record(root / 'updates/status.json'),
             'pending': bool((root / 'updates/pending.json').exists()), 'applied': False,
             'note': '현재 설치·대기 업데이트 상태입니다. 업데이트 적용은 이 메뉴에서 실행하지 않습니다.'}
    if 'jetlink-server.service' in units:
      value['latest'] = run(['jetlink', 'update', '--check'], timeout=4)
    elif 'carrot-jetlink.service' in units:
      try:
        value['latest'] = latest_carrot(root, units)
      except (OSError, ValueError) as error:
        value['latest'] = {'available': None, 'error': str(error)[:240], 'applied': False}
    return {'ok': True, 'update': value}
  if action == 'video_test':
    first = hud_receipt()
    latest = first
    deadline = time.monotonic() + 2
    while not latest.get('decoded') and time.monotonic() < deadline:
      time.sleep(.12)
      latest = hud_receipt()
    seen = latest.get('decoded') is True
    jpeg_observed = any(item.get('image_envelope_ok') is True for item in (first, latest))
    note = ('최근 JPEG 수신 및 디코딩 확인' if seen else
            'JPEG 데이터 수신 확인 · Pillow 없음 또는 디코딩 실패로 영상 검증은 미완료' if jpeg_observed else
            '2초 관찰에서 최근 JPEG 수신을 확인하지 못했습니다.')
    return {'ok': True, 'video_received': seen, 'image_envelope_ok': jpeg_observed,
            'decoded': seen, 'before': first, 'after': latest,
            'decode_error': latest.get('decode_error') or first.get('decode_error'), 'note': note}
  first = collect(units, root)
  if action == 'model':
    return {'ok': True, 'installed_model': installed_model(root), 'services': first['services'],
            'engine': first['control'].get('engine'), 'link': first['control'].get('link')}
  if action == 'settings':
    # Deliberately omit Wi-Fi profiles, environment files, passwords and SSH keys.
    first['settings'] = {'transport': 'USB host', 'runtime': str(root),
                         'protected_storage': Path('/etc/carrot-jetlink-protected.json').exists(),
                         'service_auto_start': {u: v.get('UnitFileState', 'unknown') for u, v in units.items()}}
  return {'ok': True, 'status': first}


if __name__ == '__main__':
  try:
    result = handle(sys.argv[1] if len(sys.argv) > 1 else 'status')
  except Exception as error:
    result = {'ok': False, 'error': f'{type(error).__name__}: {error}'[:400]}
  print(json.dumps(result, ensure_ascii=False, allow_nan=False))
