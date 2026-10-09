#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time

import nexo_can_diag_download_base as base

JETLINK = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_jetlink_diag.py"


def _jetlink_report() -> str:
  try:
    proc = subprocess.run(
      [sys.executable, JETLINK],
      cwd="/data/openpilot",
      capture_output=True,
      text=True,
      timeout=5.0,
    )
    text = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
    if text:
      return text
    return f"[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단\nJetlink 추가 진단 출력 없음 exit_code={proc.returncode}"
  except Exception as e:
    return (
      "[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단\n"
      f"Jetlink 추가 진단 실패: {type(e).__name__}: {e}\n"
      "※ Jetlink 추가 진단 실패는 기존 8초 통합진단 완료 여부에 영향을 주지 않습니다."
    )


def _insert_before_completion(tmp_path: str, section: str) -> None:
  with open(tmp_path, "r", encoding="utf-8", errors="replace") as src:
    lines = src.readlines()

  marker_index = None
  for index in range(len(lines) - 1, -1, -1):
    if lines[index].startswith("NEXO_DIAG_COMPLETE") or lines[index].startswith("NEXO_DIAG_FAILED"):
      marker_index = index
      break

  section_text = "\n\n" + section.rstrip() + "\n"
  if marker_index is None:
    text = "".join(lines).rstrip() + section_text
  else:
    text = "".join(lines[:marker_index]).rstrip() + section_text + "\n" + "".join(lines[marker_index:])

  with open(tmp_path, "w", encoding="utf-8") as report:
    report.write(text)
    report.flush()
    os.fsync(report.fileno())


def worker(tmp_path: str) -> int:
  """Run the preserved 8-second collectors and add a read-only Jetlink section."""
  patched_diag = None
  patched_forensic = None
  try:
    warmup = base._wait_for_control_stack()
    patched_diag = base._make_compatible_diag(tmp_path)
    patched_forensic = base._make_compatible_forensic(tmp_path)
    core_rc, timeline_rc = base._run_parallel(patched_diag, patched_forensic, tmp_path, warmup)

    # Add Jetlink only after all existing collectors are complete, but before
    # the report is atomically published. This keeps the old diagnostic logic
    # untouched and prevents a partial [31] section from being downloaded.
    try:
      _insert_before_completion(tmp_path, _jetlink_report())
    except Exception as e:
      try:
        _insert_before_completion(
          tmp_path,
          "[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단\n"
          f"Jetlink 결과 병합 실패: {type(e).__name__}: {e}\n"
          "※ 기존 8초 통합진단 결과는 그대로 유지합니다.",
        )
      except Exception:
        pass

    os.replace(tmp_path, base.REPORT)
    return 0 if core_rc == 0 and timeline_rc == 0 else 1
  except Exception as e:
    base._publish_failure(tmp_path, f"{type(e).__name__}: {e}")
    return 1
  finally:
    for patched_path in (patched_diag, patched_forensic):
      if patched_path:
        try:
          os.remove(patched_path)
        except Exception:
          pass


def main() -> int:
  try:
    os.makedirs(os.path.dirname(base.REPORT), exist_ok=True)
    try:
      os.remove(base.REPORT)
    except FileNotFoundError:
      pass

    token = f"{os.getpid()}-{time.time_ns()}"
    tmp_path = f"{base.REPORT}.{token}.tmp"
    proc = subprocess.Popen(
      [sys.executable, os.path.abspath(__file__), "--worker", tmp_path],
      cwd="/data/openpilot",
      stdin=subprocess.DEVNULL,
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      start_new_session=True,
      close_fds=True,
    )
    print(f"NEXO_DIAG_STARTED pid={proc.pid} file=/download/nexo-8sec-diagnostic.txt")
  except Exception as e:
    print(f"NEXO_DIAG_START_FAILED {type(e).__name__}: {e}")
    return 1
  return 0


if __name__ == "__main__":
  if len(sys.argv) == 3 and sys.argv[1] == "--worker":
    raise SystemExit(worker(sys.argv[2]))
  raise SystemExit(main())
