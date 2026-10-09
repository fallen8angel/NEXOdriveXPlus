#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

from jetson_status_beacon import build_payload

JETLINK_ENABLE = Path("/data/nexo_jetlink_enabled")
JETLINK_STATUS = Path("/dev/shm/nexo-jetlink.json")
JETLINK_MODEL_STATUS = Path("/dev/shm/nexo-jetlink-model.json")
JETLINK_SPEC = Path("/dev/shm/nexo-jetlink-spec.json")
JETLINK_FAULT = Path("/dev/shm/nexo-jetlink-fault")
JETLINK_SOCKET = Path("/dev/shm/nexo-jetlink.sock")
JETLINK_GADGET = Path("/sys/kernel/config/usb_gadget/jetlink")
JETLINK_FFS = Path("/dev/ffs-jetlink")


def run(args: list[str], timeout: float = 2.0) -> str:
  try:
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True, timeout=timeout).strip()
  except subprocess.CalledProcessError as e:
    return (e.output or "").strip()
  except Exception as e:
    return f"<error: {e}>"


def yn(value: object) -> str:
  return "OK" if bool(value) else "NO"


def read_text(path: Path, limit: int = 500) -> str:
  try:
    return path.read_text(encoding="utf-8", errors="replace").strip()[:limit]
  except Exception:
    return ""


def read_json(path: Path) -> dict:
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}


def proc_lines(pattern: str) -> list[str]:
  out = run(["pgrep", "-af", pattern], timeout=1.0)
  if not out or out.startswith("<error:"):
    return []
  return [line for line in out.splitlines() if line.strip()]


def param_bool_file(name: str) -> bool | None:
  path = Path("/data/params/d") / name
  if not path.exists():
    return None
  raw = read_text(path, 32).lower()
  if raw in ("1", "true", "yes", "on"):
    return True
  if raw in ("0", "false", "no", "off", ""):
    return False
  return None


def build_jetlink_status() -> dict:
  comma_side = Path("/data/openpilot").exists() or Path("/data/params/d").exists()
  daemon_processes = proc_lines(r"openpilot\.selfdrive\.modeld\.jetlink\.daemon")
  server_processes = proc_lines(r"jetlink\.server\.main")

  server_systemd = run(
    ["systemctl", "show", "nexo-jetlink-server.service", "-p", "ActiveState", "-p", "SubState"],
    timeout=1.5,
  )
  server_active = "ActiveState=active" in server_systemd
  usb_list = run(["lsusb"], timeout=1.5)
  usb_peer = "1209:0001" in usb_list.lower()

  status = read_json(JETLINK_STATUS)
  model_status = read_json(JETLINK_MODEL_STATUS)
  spec = read_json(JETLINK_SPEC)
  fault = read_text(JETLINK_FAULT, 300)
  display_usb = param_bool_file("NexoJetsonUsb")

  udc = read_text(JETLINK_GADGET / "UDC", 128) if JETLINK_GADGET.exists() else ""
  socket_ok = False
  try:
    socket_ok = JETLINK_SOCKET.exists() and stat.S_ISSOCK(JETLINK_SOCKET.stat().st_mode)
  except OSError:
    pass

  state = str(status.get("state") or "")
  status_sha = str(status.get("sha256") or "")
  spec_sha = str(spec.get("sha256") or "")
  model_sha = str(model_status.get("sha256") or "")
  model_ready = bool(model_status.get("ready"))
  model_active = bool(model_status.get("active"))
  exact_model = bool(spec_sha) and (not status_sha or status_sha == spec_sha) and (not model_sha or model_sha == spec_sha)

  enabled = JETLINK_ENABLE.exists() if comma_side else False
  conflict = bool(comma_side and enabled and display_usb is True)
  ready = bool(
    comma_side
    and enabled
    and not conflict
    and not fault
    and daemon_processes
    and JETLINK_GADGET.exists()
    and udc
    and JETLINK_FFS.exists()
    and socket_ok
    and state == "ready"
    and model_ready
    and exact_model
  )

  if comma_side:
    if not enabled:
      diagnosis = "[대기] Jetlink 기능 비활성"
    elif conflict:
      diagnosis = "[오류] NexoJetsonUsb와 Jetlink가 동시에 설정됨"
    elif fault:
      diagnosis = f"[오류] Jetlink fault: {fault}"
    elif ready and model_active:
      diagnosis = "[정상] Jetlink 모델 오프로딩 동작 중"
    elif ready:
      diagnosis = "[정상] Jetlink 모델 오프로딩 준비 완료"
    elif state == "retrying":
      error = str(status.get("error") or "Jetson 연결 재시도 중")
      diagnosis = f"[미연결] Jetlink 재시도 중: {error[:220]}"
    elif state == "conflict":
      diagnosis = f"[오류] {str(status.get('error') or 'Jetlink USB 충돌')[:220]}"
    elif state == "connecting":
      diagnosis = "[대기] Jetlink Jetson 연결 중"
    elif state == "waiting_model_contract":
      diagnosis = "[대기] NEXO 모델 계약 생성 대기 중"
    else:
      diagnosis = "[미연결] Jetlink가 활성화됐지만 모델 오프로딩 준비가 완료되지 않음"
  else:
    if server_active or server_processes:
      if usb_peer:
        diagnosis = "[Jetson] Jetlink 서버 실행 중 · comma USB 감지"
      else:
        diagnosis = "[Jetson] Jetlink 서버 실행 중 · comma USB 연결 대기"
    else:
      diagnosis = "[Jetson] Jetlink 서버 미실행"

  return {
    "side": "comma" if comma_side else "jetson",
    "enabled": enabled,
    "display_usb": display_usb,
    "conflict": conflict,
    "daemon_proc": bool(daemon_processes),
    "server_proc": bool(server_processes),
    "server_systemd_active": server_active,
    "usb_peer_1209_0001": usb_peer,
    "gadget": JETLINK_GADGET.exists(),
    "udc": udc,
    "ffs": JETLINK_FFS.exists(),
    "ipc_socket": socket_ok,
    "state": state,
    "model_ready": model_ready,
    "model_active": model_active,
    "sha256": spec_sha or status_sha or model_sha,
    "model_nbytes": spec.get("nbytes"),
    "checkpoint": spec.get("checkpoint"),
    "fault": fault,
    "error": str(status.get("error") or model_status.get("error") or ""),
    "ready": ready,
    "diagnosis": diagnosis,
  }


def print_jetlink(status: dict) -> None:
  print("=== JETLINK MODEL OFFLOAD ===")
  print(
    f"side={status.get('side')} enabled={yn(status.get('enabled'))} "
    f"display_usb={status.get('display_usb')} conflict={yn(status.get('conflict'))}"
  )
  print(
    f"daemon={yn(status.get('daemon_proc'))} server={yn(status.get('server_proc') or status.get('server_systemd_active'))} "
    f"usb_peer={yn(status.get('usb_peer_1209_0001'))} gadget={yn(status.get('gadget'))} "
    f"udc={status.get('udc') or '-'} ffs={yn(status.get('ffs'))} ipc={yn(status.get('ipc_socket'))}"
  )
  print(
    f"state={status.get('state') or '-'} model_ready={yn(status.get('model_ready'))} "
    f"model_active={yn(status.get('model_active'))} ready={yn(status.get('ready'))}"
  )
  sha = str(status.get("sha256") or "")
  print(
    f"model_sha256={sha or '-'} nbytes={status.get('model_nbytes')} "
    f"checkpoint={status.get('checkpoint') or '-'}"
  )
  if status.get("fault"):
    print(f"fault={status.get('fault')}")
  if status.get("error"):
    print(f"error={status.get('error')}")
  print(f"diagnosis={status.get('diagnosis')}")


def main() -> int:
  parser = argparse.ArgumentParser(description="NEXO Jetson / Jetlink 8-second diagnostic")
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
  jetlink = build_jetlink_status()

  print()
  print("=== SUMMARY ===")
  print(f"pipeline={final.get('pipeline')} mode={final.get('pipeline_mode')}")
  print(f"comma_ip={final.get('comma_ip') or '-'} jetson_ip={final.get('ip') or '-'}")
  print(f"ready_samples={ready_count}/{len(samples)} conflict_samples={conflict_count}/{len(samples)}")
  print(f"last_encode_id={final.get('last_encode_id')} det={final.get('last_det')} infer_ms={final.get('last_infer_ms')}")
  print(f"diagnosis={final.get('diagnosis')}")

  print()
  print_jetlink(jetlink)

  print()
  print("=== PROCESS ===")
  print(run(["pgrep", "-af", "orin_yolo_direct.py|orin_frame_bridge.py|orin_yolo_worker.py|jetson_status_beacon.py|cereal/messaging/bridge|openpilot.selfdrive.modeld.jetlink.daemon|jetlink.server.main"]))

  print()
  print("=== SYSTEMD ===")
  print(run(["systemctl", "show", "nexo-yolo.service", "-p", "ActiveState", "-p", "SubState", "-p", "ExecStart"]))
  print(run(["systemctl", "show", "nexo-jetlink-server.service", "-p", "ActiveState", "-p", "SubState", "-p", "ExecStart"]))

  print()
  print("=== RECENT JOURNAL ===")
  print(run(["journalctl", "-u", "nexo-yolo.service", "-n", "40", "-o", "cat", "--no-pager"], timeout=3.0))
  print(run(["journalctl", "-u", "nexo-jetlink-server.service", "-n", "40", "-o", "cat", "--no-pager"], timeout=3.0))

  if args.json:
    print()
    print("=== FINAL JSON ===")
    result = dict(final)
    result["jetlink"] = jetlink
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

  return 0 if final.get("ready") or jetlink.get("ready") else 2


if __name__ == "__main__":
  raise SystemExit(main())
