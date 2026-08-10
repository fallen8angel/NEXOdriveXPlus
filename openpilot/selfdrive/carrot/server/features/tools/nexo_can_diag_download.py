#!/usr/bin/env python3
from __future__ import annotations

import os
import runpy
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


def _run_diag_compat() -> int:
  """Run the diagnostic with a compatibility shim for older Params.get APIs."""
  try:
    from openpilot.common.params import Params

    original_get = Params.get

    def compat_get(self, key, *args, **kwargs):
      encoding = kwargs.pop("encoding", None)
      value = original_get(self, key, *args, **kwargs)
      if encoding and isinstance(value, (bytes, bytearray)):
        return bytes(value).decode(encoding, errors="replace")
      return value

    Params.get = compat_get
    try:
      runpy.run_path(DIAG, run_name="__main__")
    except SystemExit as e:
      code = e.code
      if code is None:
        return 0
      if isinstance(code, int):
        return code
      return 1
    return 0
  except Exception as e:
    print("=" * 68)
    print("NEXOdriveXPlus 8초 통합진단")
    print("=" * 68)
    print(f"호환 실행 오류: {type(e).__name__}: {e}")
    return 1


def worker(tmp_path: str) -> int:
  """Run the collector fully, then atomically publish one complete report."""
  try:
    with open(tmp_path, "w", encoding="utf-8") as report:
      old_stdout = sys.stdout
      old_stderr = sys.stderr
      sys.stdout = report
      sys.stderr = report
      try:
        rc = _run_diag_compat()
      finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

      if rc == 0:
        report.write("\nNEXO_DIAG_COMPLETE\n")
      else:
        report.write(f"\nNEXO_DIAG_FAILED exit_code={rc}\n")
      report.flush()
      os.fsync(report.fileno())

    # Never expose a half-written diagnostic file to the web UI.
    os.replace(tmp_path, REPORT)
    return rc
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
