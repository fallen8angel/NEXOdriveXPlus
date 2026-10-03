#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import socket
import subprocess
import time

STATUS_PORT = 8766
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


def comma_ip_from_bridge() -> str:
  out = run(["pgrep", "-af", "openpilot/cereal/messaging/bridge"], timeout=1.0)
  for line in out.splitlines():
    m = re.search(r"bridge\s+(\d{1,3}(?:\.\d{1,3}){3})\s+roadEncodeData(?:\s|$)", line)
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


def connection_state(comma_ip: str) -> tuple[bool, bool]:
  text = run(["ss", "-Htn"], timeout=1.0)
  local_pipe = False
  comma_tcp = False
  for line in text.splitlines():
    if not line.startswith("ESTAB"):
      continue
    if ":8765" in line:
      local_pipe = True
    if comma_ip and comma_ip in line:
      comma_tcp = True
  return local_pipe, comma_tcp


def recent_runtime_status() -> dict:
  text = run(
    ["journalctl", "-u", "nexo-yolo.service", "--since", "15 seconds ago", "-o", "cat", "--no-pager"],
    timeout=2.0,
  )
  frame_seen = "[bridge] first iframe received" in text
  matches = list(re.finditer(r"\[yolo\]\s+encodeId=(\d+)\s+det=(\d+)\s+infer=([0-9.]+)ms", text))
  if not matches:
    return {"frame_seen": frame_seen, "yolo_recent": False}
  m = matches[-1]
  return {
    "frame_seen": frame_seen,
    "yolo_recent": True,
    "last_encode_id": int(m.group(1)),
    "last_det": int(m.group(2)),
    "last_infer_ms": float(m.group(3)),
    "yolo_age_ms": "<15000",
  }


def build_payload() -> dict:
  comma_ip = comma_ip_from_bridge()
  local_pipe, comma_tcp = connection_state(comma_ip)
  runtime = recent_runtime_status()
  payload = {
    "magic": MAGIC,
    "version": 1,
    "ts": time.time(),
    "host": socket.gethostname(),
    "ip": local_ip(comma_ip),
    "comma_ip": comma_ip,
    "service_active": run(["systemctl", "is-active", "nexo-yolo.service"], timeout=1.0) == "active",
    "bridge_proc": proc_alive(r"openpilot/cereal/messaging/bridge .* roadEncodeData"),
    "frame_bridge_proc": proc_alive(r"orin_frame_bridge\.py"),
    "yolo_proc": proc_alive(r"orin_yolo_worker\.py"),
    "local_pipe": local_pipe,
    "comma_tcp": comma_tcp,
  }
  payload.update(runtime)
  return payload


def main() -> int:
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

  while True:
    payload = build_payload()
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    targets = [("255.255.255.255", STATUS_PORT)]
    comma_ip = str(payload.get("comma_ip") or "")
    if comma_ip:
      targets.append((comma_ip, STATUS_PORT))
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
