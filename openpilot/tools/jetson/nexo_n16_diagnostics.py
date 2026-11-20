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
  elif 'memory reserve' in reason:
    code = 'host_memory_guard'
  elif any(term in reason for term in ('out of memory', 'cudaerrormemoryallocation')):
    code = 'cuda_allocation_failed'
  elif state == 'failed':
    code = 'engine_build_or_validation_failed'
  else:
    code = 'no_verified_gpu_inference'
  return {'phase': value.get('phase', 'not_run'), 'diagnosis_code': code,
          'synthetic_gpu_verified': code == 'synthetic_only',
          'production_activation_allowed': False,
          'safe_next_step': ('separate accuracy, camera and HUD projection validation'
                             if code == 'synthetic_only' else
                             'analyze bounded memory timeline before any new engine trial'),
          'repeat_same_build_recommended': False}


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
