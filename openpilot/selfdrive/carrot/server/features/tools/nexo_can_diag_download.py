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


def _make_compatible_diag(tmp_path: str) -> str:
  """Create a temporary diagnostic copy compatible with older Params.get APIs."""
  patched_path = tmp_path + ".py"
  with open(DIAG, "r", encoding="utf-8") as src_file:
    src = src_file.read()

  replacements = {
    "params.get('CarSelected3', encoding='utf-8')": "_param_text(params, 'CarSelected3')",
    "params.get(\"CarSelected3\", encoding=\"utf-8\")": "_param_text(params, 'CarSelected3')",
    "params.get('CarName', encoding='utf-8')": "_param_text(params, 'CarName')",
    "params.get(\"CarName\", encoding=\"utf-8\")": "_param_text(params, 'CarName')",
  }
  for old, new in replacements.items():
    src = src.replace(old, new)

  helper = '''\n\ndef _param_text(params, key):\n  try:\n    value = params.get(key)\n  except Exception:\n    return None\n  if isinstance(value, (bytes, bytearray)):\n    return bytes(value).decode("utf-8", errors="replace")\n  return value\n\n'''
  marker = "def main():"
  if marker not in src:
    raise RuntimeError("diagnostic main() marker not found")
  src = src.replace(marker, helper + marker, 1)

  if "encoding='utf-8'" in src or 'encoding="utf-8"' in src:
    raise RuntimeError("unsupported Params.get encoding call remains in diagnostic")

  with open(patched_path, "w", encoding="utf-8") as patched:
    patched.write(src)
  return patched_path


def worker(tmp_path: str) -> int:
  """Run the collector fully, then atomically publish one complete report."""
  patched_diag = None
  try:
    patched_diag = _make_compatible_diag(tmp_path)
    with open(tmp_path, "w", encoding="utf-8") as report:
      proc = subprocess.run(
        [sys.executable, patched_diag],
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

    os.replace(tmp_path, REPORT)
    return 0 if proc.returncode == 0 else proc.returncode
  except Exception as e:
    _publish_failure(tmp_path, f"{type(e).__name__}: {e}")
    return 1
  finally:
    if patched_diag:
      try:
        os.remove(patched_diag)
      except FileNotFoundError:
        pass
      except Exception:
        pass


def main() -> int:
  try:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    try:
      os.remove(REPORT)
    except FileNotFoundError:
      pass

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
