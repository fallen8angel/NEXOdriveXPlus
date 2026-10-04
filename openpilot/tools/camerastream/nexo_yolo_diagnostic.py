#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
from typing import Any

MAGIC = "NEXO_JETSON_STATUS"
DEFAULT_PORT = 8766


def classify(payload: dict[str, Any]) -> tuple[str, str]:
  pipeline = str(payload.get("pipeline") or "")
  direct_mode = bool(payload.get("direct_mode") or pipeline == "direct_roadEncodeData")
  service_active = bool(payload.get("service_active"))
  yolo_proc = bool(payload.get("yolo_proc"))
  comma_tcp = bool(payload.get("comma_tcp"))
  frame_seen = bool(payload.get("camera_frame_seen", payload.get("frame_seen", False)))
  yolo_recent = bool(payload.get("yolo_recent"))
  ready = bool(payload.get("ready"))

  if direct_mode and ready:
    return "정상", "direct roadEncodeData → HEVC → YOLO 경로가 정상 동작 중입니다."
  if pipeline.startswith("legacy") or payload.get("frame_bridge_proc") or payload.get("local_pipe"):
    return "전환 필요", "젯슨은 보이지만 기존 frame bridge/local pipe 경로가 남아 있습니다."
  if not service_active or not yolo_proc:
    return "서비스 확인", "nexo-yolo.service 또는 direct YOLO 프로세스가 실행되지 않았습니다."
  if not comma_tcp:
    return "통신 대기", "젯슨 프로세스는 실행 중이지만 콤마 TCP 연결이 확인되지 않습니다."
  if not frame_seen:
    return "영상 대기", "콤마 TCP는 연결됐지만 첫 I-frame/HEVC 디코딩 프레임이 확인되지 않습니다."
  if not yolo_recent:
    return "추론 대기", "영상 프레임은 확인됐지만 최근 YOLO 추론 결과가 없습니다."
  return "재점검", "연결 요소 일부는 확인됐지만 ready 조건을 모두 만족하지 못했습니다."


def listen_status(seconds: float, port: int) -> tuple[dict[str, Any] | None, int]:
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  sock.settimeout(0.5)
  try:
    sock.bind(("0.0.0.0", port))
  except OSError as e:
    raise RuntimeError(f"UDP {port} 포트를 열 수 없습니다: {e}") from e

  deadline = time.monotonic() + seconds
  last: dict[str, Any] | None = None
  count = 0

  try:
    while time.monotonic() < deadline:
      try:
        data, addr = sock.recvfrom(65535)
      except socket.timeout:
        continue

      try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
      except Exception:
        continue

      if payload.get("magic") != MAGIC:
        continue

      payload["_source_ip"] = addr[0]
      last = payload
      count += 1
  finally:
    sock.close()

  return last, count


def main() -> int:
  parser = argparse.ArgumentParser(description="NEXO Jetson direct pipeline 8-second diagnostic")
  parser.add_argument("--seconds", type=float, default=8.0)
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--json", action="store_true")
  args = parser.parse_args()

  print("[Jetson Orin · YOLO direct 연결 진단]")
  print(f"관측시간={args.seconds:.1f}초 | status_port={args.port}")

  payload, count = listen_status(max(1.0, args.seconds), args.port)
  if payload is None:
    print("heartbeat=0")
    print("[미연결] NEXO_JETSON_STATUS heartbeat를 받지 못했습니다.")
    return 2

  status, reason = classify(payload)
  age_ms = max(0.0, (time.time() - float(payload.get("ts") or time.time())) * 1000.0)

  print(
    f"heartbeat={count} | source_ip={payload.get('_source_ip','')} | "
    f"reported_ip={payload.get('ip','')} | age={age_ms:.0f}ms"
  )
  print(
    f"version={payload.get('version')} | pipeline={payload.get('pipeline','unknown')} | "
    f"direct_mode={payload.get('direct_mode', False)}"
  )
  print(
    f"service_active={payload.get('service_active')} | yolo_proc={payload.get('yolo_proc')} | "
    f"comma_ip={payload.get('comma_ip','')} | comma_tcp={payload.get('comma_tcp')}"
  )
  print(
    f"iframe_seen={payload.get('iframe_seen')} | "
    f"camera_frame_seen={payload.get('camera_frame_seen', payload.get('frame_seen'))} | "
    f"yolo_recent={payload.get('yolo_recent')} | ready={payload.get('ready')}"
  )

  if "last_encode_id" in payload:
    print(
      f"last_encode_id={payload.get('last_encode_id')} | last_det={payload.get('last_det')} | "
      f"last_infer_ms={payload.get('last_infer_ms')}"
    )

  print(f"[{status}] {reason}")

  if args.json:
    safe_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    print(json.dumps(safe_payload, ensure_ascii=False, sort_keys=True))

  return 0 if status == "정상" else 1


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except KeyboardInterrupt:
    raise SystemExit(130)
