#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from typing import Any

STATUS_PORT = 8766
HUD_STATUS_PORT = 8767
MICI_STATUS_PORT = 8768
MAGIC = "NEXO_JETSON_STATUS"
INTERVAL_SECONDS = 1.0
DIRECT_STATUS_FILE = os.environ.get("NEXO_YOLO_STATUS_FILE", "/tmp/nexo-yolo-direct-status.json")
BRIDGE_YOLO_LOG = os.environ.get("NEXO_YOLO_WORKER_LOG", "/tmp/nexo-yolo-worker.log")
FRESH_PACKET_SECONDS = 3.0
FRESH_FRAME_SECONDS = 3.0
FRESH_YOLO_SECONDS = 5.0


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


def legacy_processes() -> dict[str, bool]:
  return {
    "legacy_bridge_proc": proc_alive(r"openpilot/cereal/messaging/bridge .* roadEncodeData"),
    "legacy_frame_bridge_proc": proc_alive(r"orin_frame_bridge\.py"),
    "legacy_yolo_worker_proc": proc_alive(r"orin_yolo_worker\.py"),
    "legacy_orin_yolo_proc": proc_alive(r"orin_yolo\.py"),
  }


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


def local_pipe_connected() -> bool:
  text = run(["ss", "-Htn"], timeout=1.0)
  return any(line.startswith("ESTAB") and ":8765" in line for line in text.splitlines())


def load_direct_status() -> dict[str, Any]:
  try:
    with open(DIRECT_STATUS_FILE, "r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}


def recent_bridge_yolo_log() -> str:
  try:
    if time.time() - os.path.getmtime(BRIDGE_YOLO_LOG) > FRESH_YOLO_SECONDS:
      return ""
    with open(BRIDGE_YOLO_LOG, "rb") as f:
      f.seek(0, os.SEEK_END)
      size = f.tell()
      f.seek(max(0, size - 131072), os.SEEK_SET)
      return f.read().decode("utf-8", errors="replace")
  except Exception:
    return ""


def age_ms(ts: Any, now: float) -> int | None:
  try:
    value = float(ts)
  except (TypeError, ValueError):
    return None
  if value <= 0:
    return None
  return max(0, int((now - value) * 1000.0))


def journal_runtime_status() -> dict[str, Any]:
  journal_text = run(
    ["journalctl", "-u", "nexo-yolo.service", "--since", "20 seconds ago", "-o", "cat", "--no-pager"],
    timeout=2.0,
  )
  # The service user may not be allowed to read the full system journal. The
  # launcher also tees live worker output to /tmp so readiness does not depend
  # on journal permissions.
  text = journal_text + "\n" + recent_bridge_yolo_log()

  iframe_seen = (
    "[orin-direct] got first iframe/header" in text
    or "[bridge] first iframe received" in text
  )
  frame_seen = (
    "[orin-direct] frame decoded" in text
    or "[bridge] first decoded frame" in text
  )

  matches = list(
    re.finditer(r"\[orin-direct\]\s+encodeId=(\d+)\s+det=(\d+)\s+infer=([0-9.]+)ms", text)
  )
  if not matches:
    matches = list(
      re.finditer(r"\[yolo\]\s+encodeId=(\d+)\s+det=(\d+)\s+infer=([0-9.]+)ms", text)
    )

  if not matches:
    return {
      "packet_seen": iframe_seen or frame_seen,
      "iframe_seen": iframe_seen,
      "frame_seen": frame_seen,
      "camera_frame_seen": frame_seen,
      "yolo_recent": False,
    }

  m = matches[-1]
  # A completed YOLO inference proves that an encoded packet, iframe/header and
  # decoded camera frame traversed the bridge pipeline even if the frame bridge
  # did not emit a dedicated "first decoded frame" log line.
  return {
    "packet_seen": True,
    "iframe_seen": True,
    "frame_seen": True,
    "camera_frame_seen": True,
    "yolo_recent": True,
    "last_encode_id": int(m.group(1)),
    "last_det": int(m.group(2)),
    "last_infer_ms": float(m.group(3)),
  }


def runtime_status(use_direct_status: bool) -> dict[str, Any]:
  if not use_direct_status:
    return journal_runtime_status()

  now = time.time()
  status = load_direct_status()
  if status.get("magic") != "NEXO_YOLO_DIRECT_STATUS":
    return journal_runtime_status()

  packet_age = age_ms(status.get("last_packet_ts"), now)
  frame_age = age_ms(status.get("last_frame_ts"), now)
  yolo_age = age_ms(status.get("last_yolo_ts"), now)
  updated_age = age_ms(status.get("updated_ts"), now)
  packet_fresh = int(status.get("packets_seen") or 0) > 0 and packet_age is not None and packet_age <= int(FRESH_PACKET_SECONDS * 1000)
  frame_fresh = bool(status.get("frame_seen")) and frame_age is not None and frame_age <= int(FRESH_FRAME_SECONDS * 1000)
  yolo_fresh = bool(status.get("yolo_recent")) and yolo_age is not None and yolo_age <= int(FRESH_YOLO_SECONDS * 1000)

  return {
    "direct_state": str(status.get("state") or ""),
    "packet_seen": packet_fresh,
    "iframe_seen": bool(status.get("iframe_seen")),
    "frame_seen": frame_fresh,
    "camera_frame_seen": frame_fresh,
    "yolo_recent": yolo_fresh,
    "status_age_ms": updated_age,
    "packet_age_ms": packet_age,
    "frame_age_ms": frame_age,
    "yolo_age_ms": yolo_age,
    "packets_seen": status.get("packets_seen"),
    "keyframes_seen": status.get("keyframes_seen"),
    "decoder_resets": status.get("decoder_resets"),
    "last_encode_id": status.get("last_encode_id"),
    "last_flags": status.get("last_flags"),
    "last_det": status.get("last_det"),
    "last_infer_ms": status.get("last_infer_ms"),
    "frame_count": status.get("frame_count"),
    "yolo_count": status.get("yolo_count"),
    "last_error": str(status.get("last_error") or ""),
  }


def service_exec() -> str:
  return run(["systemctl", "show", "nexo-yolo.service", "-p", "ExecStart", "--value"], timeout=1.5)


def diagnose(payload: dict[str, Any]) -> str:
  if not payload.get("service_active"):
    return "nexo-yolo.service inactive"
  if payload.get("pipeline_conflict"):
    return "direct and bridge pipelines are running together"

  if payload.get("direct_mode"):
    if not payload.get("comma_ip"):
      return "comma IP not detected"
    if not payload.get("comma_tcp"):
      return "no established TCP session to comma"
    if not payload.get("packet_seen"):
      return "TCP connected but no fresh roadEncodeData packets"
    if not payload.get("iframe_seen"):
      return "roadEncodeData packets received; waiting for first HEVC iframe/header"
    if not payload.get("camera_frame_seen"):
      return "iframe/header received but decoded camera frames are stale or missing"
    if not payload.get("yolo_recent"):
      return "decoded frames are present but YOLO inference is stale or not running"
    return "ready"

  if not payload.get("bridge_proc"):
    return "roadEncodeData bridge is not running"
  if not payload.get("comma_ip"):
    return "comma IP not detected"
  if not payload.get("comma_tcp"):
    return "no established TCP session to comma"
  if not payload.get("frame_bridge_proc"):
    return "orin_frame_bridge.py is not running"
  if not payload.get("yolo_proc"):
    return "YOLO worker is not running"
  if not payload.get("local_pipe"):
    return "waiting for YOLO worker on 127.0.0.1:8765"
  if not payload.get("yolo_recent"):
    return "pipeline connected but YOLO has not completed a recent inference"
  return "ready"


def build_payload() -> dict[str, Any]:
  direct_proc = direct_yolo_process()
  legacy_proc = legacy_yolo_process()
  legacy = legacy_processes()
  bridge_mode = bool(
    legacy["legacy_bridge_proc"]
    and legacy["legacy_frame_bridge_proc"]
    and (legacy["legacy_yolo_worker_proc"] or legacy["legacy_orin_yolo_proc"])
  )

  comma_ip = comma_ip_from_processes()
  runtime = runtime_status(direct_proc)
  service_command = service_exec()
  service_active = run(["systemctl", "is-active", "nexo-yolo.service"], timeout=1.0) == "active"
  service_uses_direct = bool(
    direct_proc
    or "run_nexo_yolo_direct.sh" in service_command
    or "orin_yolo_direct.py" in service_command
  )
  service_uses_bridge = bool(service_active and not direct_proc and bridge_mode)
  pipeline_conflict = bool(direct_proc and any(legacy.values()))
  bridge_started = bool(legacy_proc or any(legacy.values()))
  pipeline = "direct_roadEncodeData" if direct_proc else ("bridge_roadEncodeData" if bridge_started else "stopped")

  payload: dict[str, Any] = {
    "magic": MAGIC,
    "version": 5,
    "ts": time.time(),
    "host": socket.gethostname(),
    "ip": local_ip(comma_ip),
    "comma_ip": comma_ip,
    "service_active": service_active,
    "service_uses_direct": service_uses_direct,
    "service_uses_bridge": service_uses_bridge,
    "pipeline": pipeline,
    "pipeline_mode": "direct" if direct_proc else ("bridge" if bridge_started else "stopped"),
    "direct_mode": bool(direct_proc),
    "yolo_proc": bool(direct_proc or legacy_proc),
    "comma_tcp": comma_tcp_connected(comma_ip),
    # These compatibility fields are retained for older comma/HUD consumers.
    "bridge_proc": legacy["legacy_bridge_proc"],
    "frame_bridge_proc": legacy["legacy_frame_bridge_proc"],
    "local_pipe": local_pipe_connected() if bridge_started else False,
    "pipeline_conflict": pipeline_conflict,
  }
  payload.update(legacy)
  payload.update(runtime)

  # A recent bridge-mode YOLO inference is end-to-end proof that encoded camera
  # data crossed the comma bridge, decoded successfully and reached the worker.
  if bridge_mode and payload.get("yolo_recent"):
    payload["packet_seen"] = True
    payload["iframe_seen"] = True
    payload["frame_seen"] = True
    payload["camera_frame_seen"] = True

  direct_ready = bool(
    payload["service_active"]
    and payload["direct_mode"]
    and payload["yolo_proc"]
    and not payload["pipeline_conflict"]
    and payload["comma_tcp"]
    and payload.get("packet_seen")
    and payload.get("camera_frame_seen")
    and payload.get("yolo_recent")
  )
  bridge_ready = bool(
    payload["service_active"]
    and not payload["direct_mode"]
    and bridge_mode
    and payload["comma_tcp"]
    and payload["local_pipe"]
    and payload.get("yolo_recent")
    and not payload["pipeline_conflict"]
  )
  payload["ready"] = bool(direct_ready or bridge_ready)
  payload["diagnosis"] = diagnose(payload)

  if payload["ready"]:
    payload["state"] = "ready"
  elif payload["pipeline_conflict"]:
    payload["state"] = "pipeline_conflict"
  elif not payload["service_active"]:
    payload["state"] = "service_or_yolo_down"
  elif payload["direct_mode"]:
    if not payload["comma_tcp"]:
      payload["state"] = "comma_tcp_wait"
    elif not payload.get("packet_seen"):
      payload["state"] = "road_encode_wait"
    elif not payload.get("iframe_seen"):
      payload["state"] = "iframe_wait"
    elif not payload.get("camera_frame_seen"):
      payload["state"] = "decode_wait"
    elif not payload.get("yolo_recent"):
      payload["state"] = "yolo_inference_wait"
    else:
      payload["state"] = "not_ready"
  elif not payload["bridge_proc"] or not payload["frame_bridge_proc"] or not payload["yolo_proc"]:
    payload["state"] = "bridge_pipeline_down"
  elif not payload["comma_tcp"]:
    payload["state"] = "comma_tcp_wait"
  elif not payload["local_pipe"]:
    payload["state"] = "local_pipe_wait"
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
