"""Bounded read-only diagnostics and fail-closed N16 observer readiness.

No camera/HUD/control data is emitted. RAM and GPU memory are shared on Orin;
MemAvailable and per-process RSS are *observations*, not peak CUDA allocations.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

ROOT = Path('/opt/carrot-jetlink')
RESULT = ROOT / 'cache/nexo-n16-trial-result.json'
TIMELINE = ROOT / 'cache/nexo-n16-trial-memory.json'
MAX_SAMPLES = 256
MEMORY_FIELDS = frozenset(('MemAvailable', 'MemFree', 'MemTotal', 'SwapFree', 'SwapTotal',
                           'CmaFree', 'CmaTotal'))


def meminfo_bytes(path: Path = Path('/proc/meminfo')) -> dict[str, int]:
  result = {}
  try:
    for line in path.read_text().splitlines():
      key, sep, value = line.partition(':')
      if sep and key in MEMORY_FIELDS:
        result[key + '_bytes'] = int(value.split()[0]) * 1024
  except (OSError, ValueError, IndexError):
    pass
  return result


def _proc_tree(root_pid: int, proc: Path = Path('/proc'), maximum: int = 12) -> list[int]:
  """Bounded same-user worker tree; missing or vanished PIDs are harmless."""
  if type(root_pid) is not int or root_pid <= 0:
    return []
  seen = set()
  queue = [root_pid]
  while queue and len(seen) < maximum:
    pid = queue.pop(0)
    if pid in seen:
      continue
    seen.add(pid)
    try:
      raw = (proc / str(pid) / 'task' / str(pid) / 'children').read_text()
      for child in raw.split()[:maximum]:
        if child.isdigit() and int(child) > 0 and int(child) not in seen:
          queue.append(int(child))
    except (OSError, ValueError):
      continue
  return sorted(seen)


def proc_memory_bytes(root_pid: int, proc: Path = Path('/proc')) -> dict:
  rss, high, swap, active = 0, 0, 0, 0
  for pid in _proc_tree(root_pid, proc):
    try:
      data = (proc / str(pid) / 'status').read_text()
    except OSError:
      continue
    active += 1
    for line in data.splitlines():
      key, _, value = line.partition(':')
      if key in ('VmRSS', 'VmHWM', 'VmSwap'):
        try:
          n = int(value.split()[0]) * 1024
          if key == 'VmRSS':
            rss += n
          elif key == 'VmHWM':
            high += n
          else:
            swap += n
        except (IndexError, ValueError):
          pass
  return {'worker_processes_observed': active, 'worker_rss_bytes': rss,
          'worker_rss_highwater_sum_bytes': high, 'worker_swap_bytes': swap}


def system_sample(pid: int | None = None, *, now: float | None = None) -> dict:
  value = {'elapsed_wall_unix': round(time.time() if now is None else now, 3)}
  value.update(meminfo_bytes())
  if pid is not None:
    value.update(proc_memory_bytes(pid))
  return value


class Timeline:
  def __init__(self, limit: int = MAX_SAMPLES):
    if type(limit) is not int or not 1 <= limit <= MAX_SAMPLES:
      raise ValueError('Invalid N16 memory timeline limit')
    self.limit = limit
    self.samples = []
    self.total_samples = 0
    self.minimum_available_bytes = None
    self.maximum_worker_rss_bytes = 0

  def record(self, sample: dict, phase: str) -> None:
    entry = dict(sample)
    entry['phase'] = phase if isinstance(phase, str) and phase[:80] == phase else 'unknown'
    available = entry.get('MemAvailable_bytes')
    rss = entry.get('worker_rss_bytes')
    if type(available) is int:
      self.minimum_available_bytes = (available if self.minimum_available_bytes is None
                                     else min(self.minimum_available_bytes, available))
    if type(rss) is int:
      self.maximum_worker_rss_bytes = max(self.maximum_worker_rss_bytes, rss)
    self.samples.append(entry)
    if len(self.samples) > self.limit:
      self.samples.pop(0)
    self.total_samples += 1

  def export(self) -> dict:
    return {'schema_version': 1, 'scope': 'read_only_shared_RAM_samples_not_cuda_peak',
            'samples': list(self.samples), 'samples_taken': self.total_samples,
            'minimum_memavailable_bytes': self.minimum_available_bytes,
            'maximum_worker_rss_bytes': self.maximum_worker_rss_bytes,
            'cuda_memory_peak_verified': False, 'gpu_memory_shared_with_cpu': True}


def classify_trial(result: dict | None) -> dict:
  """No GPU/HUD approval from a partial or missing trial report."""
  value = result if isinstance(result, dict) else {}
  state = value.get('state')
  reason = str(value.get('error') or '')[:320].lower()
  if state == 'passed' and value.get('gpu_inference_verified') is True:
    code = 'synthetic_only'
  elif value.get('partition_gpu_parity') is True and state in ('gpu_parity_passed', 'gpu_parity_failed', 'timed_out'):
    code = {'gpu_parity_passed': 'offline_gpu_parity_passed', 'gpu_parity_failed': 'offline_gpu_numerical_mismatch',
            'timed_out': 'offline_gpu_trial_time_limit'}[state]
  elif value.get('partition_stateful_cpu_only') is True and (
      state == 'timed_out' or (state == 'failed' and reason.startswith('timeouterror: n16 trial time limit reached'))):
    code = 'stateful_cpu_time_limit'
  elif state == 'cpu_parity_failed':
    code = 'cpu_numerical_parity_failed'
  elif state == 'boundary_diagnosis_complete':
    code = 'cpu_boundary_diagnosis_only'
  elif 'memory reserve' in reason:
    code = 'host_memory_guard'
  elif any(term in reason for term in ('out of memory', 'cudaerrormemoryallocation')):
    code = 'cuda_allocation_failed'
  elif value.get('partition_gpu_parity') is True and state == 'failed':
    if 'passing local cut-360' in reason:
      code = 'offline_gpu_cpu_receipt_required'
    elif 'timeouterror: offline build_' in reason:
      code = 'offline_gpu_build_time_limit'
    else:
      code = 'offline_gpu_stage_failed'
  elif state == 'failed':
    code = 'engine_build_or_validation_failed'
  else:
    code = 'no_verified_gpu_inference'
  return {'phase': value.get('phase', 'not_run'), 'diagnosis_code': code,
          'synthetic_gpu_verified': code == 'synthetic_only' or (code in ('offline_gpu_parity_passed', 'offline_gpu_numerical_mismatch')
                                                                and value.get('gpu_inference_verified') is True),
          'production_activation_allowed': False,
          'safe_next_step': ('separate accuracy, camera and HUD projection validation'
                             if code == 'synthetic_only' else
                             'inspect elapsed time and last CPU frame; numerical parity is incomplete'
                             if code == 'stateful_cpu_time_limit' else
                             'inspect offline stage, CUDA samples and per-output errors; keep Display Only'
                             if code.startswith('offline_gpu_') else
                             'inspect the selected CPU cut-boundary tensors before GPU engine work'
                             if code in ('cpu_numerical_parity_failed', 'cpu_boundary_diagnosis_only') else
                             'analyze bounded memory timeline before any new engine trial'),
          'repeat_same_build_recommended': False}


def assess_feasibility(trial: dict | None, timeline: dict | None = None) -> dict:
  """Stage-3 decision receipt. Never confuse offline testing with drive approval."""
  value = trial if isinstance(trial, dict) else {}
  trace = timeline if isinstance(timeline, dict) else {}
  initial = value.get('available_memory')
  low = trace.get('minimum_memavailable_bytes')
  if type(low) is not int:
    low = value.get('minimum_memory_available_bytes')
  observed_loss = initial - low if type(initial) is int and type(low) is int and low <= initial else None
  minimum = min(initial, low) if type(initial) is int and type(low) is int else low
  diagnosis = classify_trial(value)
  return {
    'model': 'Carrot N16 2019 NEXO',
    'engine_created_and_loaded': diagnosis['synthetic_gpu_verified'],
    'synthetic_gpu_inference_verified': diagnosis['synthetic_gpu_verified'],
    'model_cpu_gpu_numerical_parity_verified': (diagnosis['diagnosis_code'] == 'offline_gpu_parity_passed'
                                                and value.get('gpu_parity_verified') is True
                                                and value.get('frames_completed') == 140 and value.get('failed_output_count') == 0),
    'camera_mapping_verified': False, 'real_vehicle_validated': False,
    'hardware_fit_proven': diagnosis['synthetic_gpu_verified'],
    'production_vehicle_approved': False,
    'initial_memavailable_bytes': initial, 'minimum_memavailable_bytes': minimum,
    'observed_available_memory_drop_bytes': observed_loss,
    'peak_cuda_allocation_bytes_measured': None,
    'diagnosis': diagnosis['diagnosis_code'],
    'recommended_action': ('calibrate and compare CPU/GPU reference on offline inputs'
                           if diagnosis['synthetic_gpu_verified'] else
                           'evaluate split-process ONNX preprocessing and bounded RAM timeline; no repeat of identical build'),
  }


def hud_observer_contract(trial: dict | None, *, numerical_accuracy_verified: bool = False,
                          camera_geometry_verified: bool = False,
                          live_camera_verified: bool = False) -> dict:
  """Read-only integration boundary. Never produces a drawable HUD overlay.

  Passing synthetic inference is insufficient to claim valid NEXO geometry.
  The observation does not switch the runtime, enable modeld, or publish CAN.
  """
  state = classify_trial(trial)
  blockers = []
  if not state['synthetic_gpu_verified']:
    blockers.append('GPU synthetic inference unverified')
  if numerical_accuracy_verified is not True:
    blockers.append('reference numerical parity unverified')
  if live_camera_verified is not True:
    blockers.append('real NEXO camera preprocessing unverified')
  if camera_geometry_verified is not True:
    blockers.append('NEXO camera-to-HUD projection unverified')
  return {'schema_version': 1, 'vehicle': 'Hyundai NEXO (FE), 2019',
          'source_model': 'Carrot N16', 'mode': 'offline_read_only_observer',
          'available_output_names': ['lane_lines', 'road_edges', 'lead', 'lead_prob', 'plan'],
          'traffic_lights_available_from_n16_contract': False,
          'blockers': blockers, 'visual_overlay_enabled': False,
          'physical_hud_feed_enabled': False, 'camera_feed_enabled': False,
          'vehicle_control_enabled': False, 'renderable_objects': [],
          'note': 'N16 raw vector outputs are not calibrated HUD objects or traffic light detections.'}


def save_json(path: Path, value: dict) -> None:
  if path.is_symlink():
    raise ValueError('Refusing to overwrite a symlinked N16 diagnostic')
  staging = path.with_name(path.name + '.tmp')
  if staging.is_symlink():
    raise ValueError('Refusing to use a symlinked temporary diagnostic')
  try:
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(staging, path)
  finally:
    staging.unlink(missing_ok=True)


def read_report(path: Path, *, limit: int = 262144) -> dict:
  if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
    return {}
  try:
    value = json.loads(path.read_text(encoding='utf-8'))
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


def summary(result_path: Path = RESULT, timeline_path: Path = TIMELINE) -> dict:
  trial = read_report(result_path)
  timeline = read_report(timeline_path, limit=1024*1024)
  return {'schema_version': 1, 'trial': trial, 'diagnosis': classify_trial(trial),
          'feasibility': assess_feasibility(trial, timeline),
          'memory_timeline': timeline, 'hud_observer': hud_observer_contract(trial),
          'physical_hud_modified': False, 'vehicle_control_modified': False}


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--status', action='store_true', help='Read previous N16 trial and observer readiness')
  args = parser.parse_args()
  if not args.status:
    parser.error('Only read-only --status is supported')
  print(json.dumps(summary(), ensure_ascii=False, indent=2))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
