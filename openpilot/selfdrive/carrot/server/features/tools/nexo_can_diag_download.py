#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys

DIAG = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py"
REPORT = "/data/media/nexo-8sec-diagnostic.txt"


def main() -> int:
  try:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    try:
      os.remove(REPORT)
    except FileNotFoundError:
      pass

    # The 7000 tools API has a 10 second shell timeout. Start the real 8 second
    # collector detached and return immediately; the web UI polls REPORT.
    with open(REPORT, "w", encoding="utf-8") as report:
      proc = subprocess.Popen(
        [sys.executable, DIAG],
        cwd="/data/openpilot",
        stdout=report,
        stderr=subprocess.STDOUT,
        start_new_session=True,
      )
    print(f"NEXO_DIAG_STARTED pid={proc.pid} file=/download/nexo-8sec-diagnostic.txt")
  except Exception as e:
    print(f"NEXO_DIAG_START_FAILED {type(e).__name__}: {e}")
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
