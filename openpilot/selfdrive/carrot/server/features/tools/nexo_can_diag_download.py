#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time

DIAG = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py"
TIMELINE = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_cruise_timeline.py"
BLINKER = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_blinker_diag.py"
LONG_DETAIL = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_long_detail_diag.py"
LONG_FORENSIC = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_long_forensic_diag.py"
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


def _run_parallel(patched_diag: str, tmp_path: str) -> tuple[int, int]:
  diag_out = tmp_path + ".core"
  timeline_out = tmp_path + ".timeline"
  blinker_out = tmp_path + ".blinker"
  long_detail_out = tmp_path + ".longdetail"
  forensic_out = tmp_path + ".forensic"

  with open(diag_out, "w", encoding="utf-8") as core_report, \
       open(timeline_out, "w", encoding="utf-8") as timeline_report, \
       open(blinker_out, "w", encoding="utf-8") as blinker_report, \
       open(long_detail_out, "w", encoding="utf-8") as long_detail_report, \
       open(forensic_out, "w", encoding="utf-8") as forensic_report:
    core_proc = subprocess.Popen(
      [sys.executable, patched_diag],
      cwd="/data/openpilot",
      stdout=core_report,
      stderr=subprocess.STDOUT,
    )
    timeline_proc = subprocess.Popen(
      [sys.executable, TIMELINE],
      cwd="/data/openpilot",
      stdout=timeline_report,
      stderr=subprocess.STDOUT,
    )
    blinker_proc = subprocess.Popen(
      [sys.executable, BLINKER],
      cwd="/data/openpilot",
      stdout=blinker_report,
      stderr=subprocess.STDOUT,
    )
    long_detail_proc = subprocess.Popen(
      [sys.executable, LONG_DETAIL],
      cwd="/data/openpilot",
      stdout=long_detail_report,
      stderr=subprocess.STDOUT,
    )
    forensic_proc = subprocess.Popen(
      [sys.executable, LONG_FORENSIC],
      cwd="/data/openpilot",
      stdout=forensic_report,
      stderr=subprocess.STDOUT,
    )
    core_rc = core_proc.wait()
    timeline_rc = timeline_proc.wait()
    blinker_rc = blinker_proc.wait()
    long_detail_rc = long_detail_proc.wait()
    forensic_rc = forensic_proc.wait()

  with open(tmp_path, "w", encoding="utf-8") as report:
    with open(diag_out, "r", encoding="utf-8", errors="replace") as src:
      report.write(src.read().rstrip())
    report.write("\n")
    if timeline_rc == 0:
      with open(timeline_out, "r", encoding="utf-8", errors="replace") as src:
        report.write(src.read().rstrip())
    else:
      report.write("\n[12] AI 비교용 MODE · MED · 속도설정 타임라인\n")
      report.write(f"타임라인 수집 실패 exit_code={timeline_rc}\n")

    report.write("\n")
    if blinker_rc == 0:
      with open(blinker_out, "r", encoding="utf-8", errors="replace") as src:
        report.write(src.read().rstrip())
    else:
      # Keep the existing 8-second diagnostic completion behavior intact even
      # if this optional add-on fails. The failure remains visible in the TXT.
      report.write("\n[17] 방향지시등 · 콤마 표시 진단\n")
      report.write(f"방향지시등 추가 진단 실패 exit_code={blinker_rc}\n")

    report.write("\n")
    if long_detail_rc == 0:
      with open(long_detail_out, "r", encoding="utf-8", errors="replace") as src:
        report.write(src.read().rstrip())
    else:
      # This is observation-only and optional. Do not change the original
      # completion result if the extra collector fails on an older build.
      report.write("\n[18] 롱컨 실제 명령 · 페달 · 정차 상태\n")
      report.write(f"롱컨 상세 추가 진단 실패 exit_code={long_detail_rc}\n")

    report.write("\n")
    if forensic_rc == 0:
      with open(forensic_out, "r", encoding="utf-8", errors="replace") as src:
        report.write(src.read().rstrip())
    else:
      # The forensic collector is also observation-only. Older installs or a
      # missing DBC must not make the original 8-second diagnostic fail.
      report.write("\n[23] SCC12 · 롱컨 포렌식 추가 진단\n")
      report.write(f"롱컨 포렌식 추가 진단 실패 exit_code={forensic_rc}\n")

    report.write("\n\n")
    if core_rc == 0 and timeline_rc == 0:
      report.write("NEXO_DIAG_COMPLETE\n")
    else:
      report.write(f"NEXO_DIAG_FAILED core={core_rc} timeline={timeline_rc}\n")
    report.flush()
    os.fsync(report.fileno())

  for path in (diag_out, timeline_out, blinker_out, long_detail_out, forensic_out):
    try:
      os.remove(path)
    except Exception:
      pass

  return core_rc, timeline_rc


def worker(tmp_path: str) -> int:
  """Run core diagnostic and comparison add-ons together, then publish one report."""
  patched_diag = None
  try:
    patched_diag = _make_compatible_diag(tmp_path)
    core_rc, timeline_rc = _run_parallel(patched_diag, tmp_path)
    os.replace(tmp_path, REPORT)
    return 0 if core_rc == 0 and timeline_rc == 0 else 1
  except Exception as e:
    _publish_failure(tmp_path, f"{type(e).__name__}: {e}")
    return 1
  finally:
    if patched_diag:
      try:
        os.remove(patched_diag)
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
