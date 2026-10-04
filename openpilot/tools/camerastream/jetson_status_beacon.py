#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import socket
import subprocess
import time

STATUS_PORT = 8766
HUD_STATUS_PORT = 8767
MICI_STATUS_PORT = 8768
MAGIC = "NEXO_JETSON_STATUS"
INTERVAL_SECONDS = 1.0


def run(args, timeout=1.5) -> str:
  try:
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True, timeout=timeout).strip()
  except Exception:
    return ""


def proc_alive(pattern: str) -> bool:
  try:
    return subprocess.run(
      ["pgrep", "-f", pattern],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      timeout=1.0,
    ).returncode == 0
  except Exception:
    return False


def direct_yolo_process() -> bool:
  return proc_alive(r"orin_yolo_direct\.py")


def legacy_yolo_process() -> bool:
  return proc_alive(r"orin_yolo\.py") or proc_alive(r"orin_yolo_worker\.py")


def comma_ip_from_processes() -> str:
  patterns = [
    r"orin_yolo_direct\.py\s+(\d{1,3}(?:\.\d{1,3}){3})(?:\s|$)",
    r"orin_yolo\.py\s+(\d{1,3}(?:\.\d{1,3}){3})(?:\s|$)",
    r"bridge\s+(\d{1,3}(?:\.\d{1,3}){3})\s+roadEncodeData(?:\s|$)",
  ]
  out = run(
    ["pgrep", "-af", "orin_yolo_direct.py|orin_yolo.py|orin_yolo_worker.py|openpilot/cereal/messaging/bridge"],
    timeout=1.0,
  )
  for pattern in patterns:
    for line in out.splitlines():
      m = re.search(pattern, line)
      if m:
        return m.group(1)
  return ""


def local_ip(target: str) -> str:
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    sock.connect((target or "8.8.8.8", 9))
    return str(sock.getsockname()[0])
  except Exception:
    return ""
  finally:
    sock.close()


def comma_tcp_connected(comma_ip: str) -> bool:
  if not comma_ip:
    return False
  text = run(["ss", "-Htn"], timeout=1.0)
  return any(line.startswith("ESTAB") and comma_ip in line for line in text.splitlines())


def recent_runtime_status() -> dict:
  text = run(
    ["journalctl", "-u", "nexo-yolo.service", "--since", "15 seconds ago", "-o", "cat", "--no-pager"],
    timeout=2.0,
  )

  iframe_seen = "[orin-direct] got first iframe/header" in text
  frame_seen = "[orin-direct] frame decoded" in text or iframe_seen

  matches = list(
    re.finditer(
      r"\[orin-direct\]\s+encodeId=(\d+)\s+det=(\d+)\s+infer=([0-9.]+)ms",
      text,
    )
  )
  if not matches:
    matches = list(
      re.finditer(
        r"\[yolo\]\s+encodeId=(\d+)\s+det=(\d+)\s+infer=([0-9.]+)ms",
        text,
      )
    )

  if not matches:
    return {
      "iframe_seen": iframe_seen,
      "frame_seen": frame_seen,
      "camera_frame_seen": frame_seen,
      "yolo_recent": False,
    }

  m = matches[-1]
  return {
    "iframe_seen": True,
    "frame_seen": True,
    "camera_frame_seen": True,
    "yolo_recent": True,
    "last_encode_id": int(m.group(1)),
    "last_det": int(m.group(2)),
    "last_infer_ms": float(m.group(3)),
    "yolo_age_ms": "<15000",
  }


def build_payload() -> dict:
  comma_ip = comma_ip_from_processes()
  runtime = recent_runtime_status()
  direct_proc = direct_yolo_process()
  legacy_proc = legacy_yolo_process()
  comma_tcp = comma_tcp_connected(comma_ip)
  service_active = run(["systemctl", "is-active", "nexo-yolo.service"], timeout=1.0) == "active"

  pipeline = "direct_roadEncodeData" if direct_proc else ("legacy_bridge" if legacy_proc else "stopped")

  payload = {
    "magic": MAGIC,
    "version": 3,
    "ts": time.time(),
    "host": socket.gethostname(),
    "ip": local_ip(comma_ip),
    "comma_ip": comma_ip,
    "service_active": service_active,
    "pipeline": pipeline,
    "direct_mode": bool(direct_proc),
    "yolo_proc": bool(direct_proc or legacy_proc),
    "comma_tcp": comma_tcp,
    # Legacy fields are retained so older comma/HUD diagnostics do not crash.
    "bridge_proc": proc_alive(r"openpilot/cereal/messaging/bridge .* roadEncodeData"),
    "frame_bridge_proc": proc_alive(r"orin_frame_bridge\.py"),
    "local_pipe": proc_alive(r"orin_yolo_worker\.py"),
  }
  payload.update(runtime)

  payload["ready"] = bool(
    payload["service_active"]
    and payload["direct_mode"]
    and payload["yolo_proc"]
    and payload["comma_tcp"]
    and payload.get("camera_frame_seen")
    and payload.get("yolo_recent")
  )

  if payload["ready"]:
    payload["state"] = "ready"
  elif not payload["service_active"] or not payload["yolo_proc"]:
    payload["state"] = "service_or_yolo_down"
  elif not payload["comma_tcp"]:
    payload["state"] = "comma_tcp_wait"
  elif not payload.get("camera_frame_seen"):
    payload["state"] = "iframe_or_decode_wait"
  elif not payload.get("yolo_recent"):
    payload["state"] = "yolo_inference_wait"
  else:
    payload["state"] = "not_ready"

  return payload


def main() -> int:
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

  while True:
    payload = build_payload()
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    targets = [
      ("255.255.255.255", STATUS_PORT),
      ("255.255.255.255", HUD_STATUS_PORT),
      ("255.255.255.255", MICI_STATUS_PORT),
    ]
    comma_ip = str(payload.get("comma_ip") or "")
    if comma_ip:
      targets.extend([
        (comma_ip, STATUS_PORT),
        (comma_ip, HUD_STATUS_PORT),
        (comma_ip, MICI_STATUS_PORT),
      ])

    for target in targets:
      try:
        sock.sendto(data, target)
      except Exception:
        pass

    time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except KeyboardInterrupt:
    raise SystemExit(0)
