#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import time

OBSERVE_SECONDS = 8.0
STATUS_PORT = 8766
MAGIC = "NEXO_JETSON_STATUS"


def b(value) -> str:
  return "True" if bool(value) else "False"


def main() -> int:
  started = time.monotonic()
  deadline = started + OBSERVE_SECONDS
  packets = 0
  last = None
  last_src = "-"
  error = ""

  sock = None
  try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", STATUS_PORT))
    sock.settimeout(0.35)

    while time.monotonic() < deadline:
      try:
        data, addr = sock.recvfrom(65535)
      except socket.timeout:
        continue
      except OSError as e:
        error = f"{type(e).__name__}: {e}"
        break

      try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
      except Exception:
        continue
      if payload.get("magic") != MAGIC:
        continue

      packets += 1
      last = payload
      last_src = str(addr[0])
  except Exception as e:
    error = f"{type(e).__name__}: {e}"
  finally:
    if sock is not None:
      try:
        sock.close()
      except Exception:
        pass

  print("[28] Jetson Orin · YOLO 연결 진단")
  print("※ 읽기 전용 상태 확인입니다. 차량 제어/CAN/Panda에는 명령을 보내지 않습니다.")

  if last is None:
    print(f"heartbeat: 0 packets / {OBSERVE_SECONDS:.0f}s")
    if error:
      print(f"listener error: {error}")
    print("[미연결] Jetson 상태 heartbeat를 받지 못했습니다.")
    print("확인: Jetson 전원 · 같은 Wi-Fi/핫스팟 · nexo-yolo.service · jetson_status_beacon.py")
    return 0

  try:
    age_ms = max(0.0, (time.time() - float(last.get("ts", 0.0))) * 1000.0)
  except Exception:
    age_ms = -1.0

  bridge_proc = bool(last.get("bridge_proc", False))
  frame_bridge_proc = bool(last.get("frame_bridge_proc", False))
  yolo_proc = bool(last.get("yolo_proc", False))
  local_pipe = bool(last.get("local_pipe", False))
  comma_tcp = bool(last.get("comma_tcp", False))
  yolo_recent = bool(last.get("yolo_recent", False))
  frame_seen = bool(last.get("frame_seen", False))

  print(
    f"heartbeat={packets} | source_ip={last_src} | reported_ip={last.get('ip','-')} | "
    f"host={last.get('host','-')} | age={age_ms:.0f}ms"
  )
  print(f"comma_ip={last.get('comma_ip','-')} | comma_tcp={b(comma_tcp)}")
  print(
    f"process: bridge={b(bridge_proc)} | frame_bridge={b(frame_bridge_proc)} | "
    f"yolo_worker={b(yolo_proc)} | local_pipe_8765={b(local_pipe)}"
  )
  print(f"camera_frame_seen={b(frame_seen)} | yolo_recent={b(yolo_recent)}")

  if last.get("last_encode_id") is not None:
    print(
      f"YOLO last: encodeId={last.get('last_encode_id')} | det={last.get('last_det','-')} | "
      f"infer={last.get('last_infer_ms','-')}ms | age={last.get('yolo_age_ms','-')}ms"
    )

  if bridge_proc and frame_bridge_proc and yolo_proc and local_pipe and comma_tcp and yolo_recent:
    print("[정상] Jetson · 콤마 영상 링크 · YOLO 추론이 모두 동작 중입니다.")
  elif bridge_proc and frame_bridge_proc and yolo_proc and local_pipe and not comma_tcp:
    print("[대기] Jetson 내부 파이프라인은 정상이며 콤마 roadEncodeData 연결을 기다리는 중입니다.")
    print("※ 차량 offroad 또는 encoderd 미실행 상태에서는 이 결과가 정상일 수 있습니다.")
  elif bridge_proc and frame_bridge_proc and yolo_proc and local_pipe and comma_tcp and not yolo_recent:
    print("[연결됨·영상대기] 콤마 TCP 링크는 있으나 최근 YOLO 추론 로그가 없습니다.")
  else:
    print("[주의] Jetson heartbeat는 수신됐지만 일부 프로세스/연결이 준비되지 않았습니다.")

  if error:
    print(f"listener note: {error}")
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("[28] Jetson Orin · YOLO 연결 진단")
    print(f"Jetson 진단 내부 오류: {type(e).__name__}: {e}")
    print("※ 추가 진단 오류가 전체 8초 진단 다운로드를 막지 않도록 종료코드는 0으로 반환합니다.")
    raise SystemExit(0)
