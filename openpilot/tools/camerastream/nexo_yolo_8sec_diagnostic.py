#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

from jetson_status_beacon import build_payload


def run(args: list[str], timeout: float = 2.0) -> str:
  try:
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True, timeout=timeout).strip()
  except subprocess.CalledProcessError as e:
    return (e.output or "").strip()
  except Exception as e:
    return f"<error: {e}>"


def yn(value: object) -> str:
  return "OK" if bool(value) else "NO"


def main() -> int:
  parser = argparse.ArgumentParser(description="NEXO Jetson direct-pipeline 8-second diagnostic")
  parser.add_argument("--seconds", type=int, default=8)
  parser.add_argument("--json", action="store_true", help="print final payload as JSON")
  args = parser.parse_args()

  seconds = max(1, min(args.seconds, 30))
  print(f"=== NEXO Jetson direct diagnostic / {datetime.now().isoformat(timespec='seconds')} ===")
  print(f"duration={seconds}s status_file={os.environ.get('NEXO_YOLO_STATUS_FILE', '/tmp/nexo-yolo-direct-status.json')}")
  print()

  samples: list[dict] = []
  for i in range(seconds):
    payload = build_payload()
    samples.append(payload)
    print(
      f"[{i + 1:02d}/{seconds:02d}] "
      f"service={yn(payload.get('service_active'))} "
      f"direct={yn(payload.get('service_uses_direct'))} "
      f"proc={yn(payload.get('direct_mode'))} "
      f"tcp={yn(payload.get('comma_tcp'))} "
      f"iframe={yn(payload.get('iframe_seen'))} "
      f"frame={yn(payload.get('camera_frame_seen'))} "
      f"yolo={yn(payload.get('yolo_recent'))} "
      f"ready={yn(payload.get('ready'))} "
      f"frame_age_ms={payload.get('frame_age_ms')} "
      f"yolo_age_ms={payload.get('yolo_age_ms')} "
      f"diag={payload.get('diagnosis')}"
    )
    if i + 1 < seconds:
      time.sleep(1)

  final = samples[-1]
  ready_count = sum(1 for x in samples if x.get("ready"))
  conflict_count = sum(1 for x in samples if x.get("pipeline_conflict"))

  print()
  print("=== SUMMARY ===")
  print(f"pipeline={final.get('pipeline')} mode={final.get('pipeline_mode')}")
  print(f"comma_ip={final.get('comma_ip') or '-'} jetson_ip={final.get('ip') or '-'}")
  print(f"ready_samples={ready_count}/{len(samples)} conflict_samples={conflict_count}/{len(samples)}")
  print(f"last_encode_id={final.get('last_encode_id')} det={final.get('last_det')} infer_ms={final.get('last_infer_ms')}")
  print(f"diagnosis={final.get('diagnosis')}")

  print()
  print("=== PROCESS ===")
  print(run(["pgrep", "-af", "orin_yolo_direct.py|orin_frame_bridge.py|orin_yolo_worker.py|jetson_status_beacon.py|cereal/messaging/bridge"]))

  print()
  print("=== SYSTEMD ===")
  print(run(["systemctl", "show", "nexo-yolo.service", "-p", "ActiveState", "-p", "SubState", "-p", "ExecStart"]))

  print()
  print("=== RECENT JOURNAL ===")
  print(run(["journalctl", "-u", "nexo-yolo.service", "-n", "40", "-o", "cat", "--no-pager"], timeout=3.0))

  if args.json:
    print()
    print("=== FINAL JSON ===")
    print(json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True))

  return 0 if final.get("ready") else 2


if __name__ == "__main__":
  raise SystemExit(main())
