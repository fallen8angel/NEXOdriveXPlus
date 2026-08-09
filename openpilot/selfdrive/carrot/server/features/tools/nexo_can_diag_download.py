#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys

CMD = [sys.executable, "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py"]


def main() -> int:
  try:
    proc = subprocess.run(CMD, capture_output=True, text=True, timeout=30)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
      print(out)
    elif err:
      print("NEXOdriveXPlus 8초 통합진단 실행 오류")
      print(err)
    else:
      print("NEXOdriveXPlus 8초 통합진단 결과가 비어 있습니다.")
  except Exception as e:
    print("NEXOdriveXPlus 8초 통합진단 실행 오류")
    print(f"{type(e).__name__}: {e}")
  # 진단 판정이 주의/주행금지여도 웹 다운로드는 성공해야 한다.
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
