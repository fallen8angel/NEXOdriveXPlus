#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time

DIAG = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py"
REPORT = "/data/media/nexo-8sec-diagnostic.txt"


def _publish_failure(tmp_path: str, message: str) -> None:
  try:
    with open(tmp_path, "w", encoding="utf-8") as report:
      report.write("NEXOdriveXPlus 8초 통합진단\n")
      report.write("=" * 68 + "\n")
      report.write(f"진단 실행기 오류: {message}\n")
      report.write("NEXO_DIAG_FAILED\n")
      report.flush()
      os.fsync(report.fileno())
    os.replace(tmp_path, REPORT)
  except Exception:
    pass


def worker(tmp_path: str) -> int:
  """Run the collector fully, then atomically publish one complete report."""
  try:
    with open(tmp_path, "w", encoding="utf-8") as report:
      proc = subprocess.run(
        [sys.executable, DIAG],
        cwd="/data/openpilot",
        stdout=report,
        stderr=subprocess.STDOUT,
        check=False,
      )
      if proc.returncode == 0:
        report.write("\nNEXO_DIAG_COMPLETE\n")
      else:
        report.write(f"\nNEXO_DIAG_FAILED exit_code={proc.returncode}\n")
      report.flush()
      os.fsync(report.fileno())

    # Never expose a half-written diagnostic file to the web UI.
    os.replace(tmp_path, REPORT)
    return 0 if proc.returncode == 0 else proc.returncode
  except Exception as e:
    _publish_failure(tmp_path, f"{type(e).__name__}: {e}")
    return 1


def main() -> int:
  try:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    try:
      os.remove(REPORT)
    except FileNotFoundError:
      pass

    # The 7000 tools API has a short shell timeout. Start a detached worker and
    # return immediately. The worker writes to a unique temporary file and only
    # publishes REPORT after the full diagnostic has finished.
    token = f"{os.getpid()}-{time.time_ns()}"
    tmp_path = f"{REPORT}.{token}.tmp"
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
