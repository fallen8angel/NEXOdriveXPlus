#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import subprocess
import time

OBSERVE_SECONDS = 8.0
STATUS_PORT = 8766
MAGIC = "NEXO_JETSON_STATUS"
REPO_ROOT = "/data/openpilot"
BOOT_CRITICAL_PROCS = ("manager", "ui", "pandad", "card", "selfdrived", "controlsd", "radard", "modeld")
BOOT_ERROR_TOKENS = (
  "traceback (most recent call last):",
  "modulenotfounderror:",
  "importerror:",
  "syntaxerror:",
  "nameerror:",
  "attributeerror:",
  "runtimeerror:",
  "typeerror:",
  "valueerror:",
  "assertionerror:",
  "segmentation fault",
  "core dumped",
  "fatal error:",
  "killed",
)


def b(value) -> str:
  return "True" if bool(value) else "False"


def _run_cmd(args: list[str], timeout: float = 2.0) -> tuple[int, str]:
  try:
    proc = subprocess.run(
      args,
      cwd=REPO_ROOT,
      capture_output=True,
      text=True,
      timeout=timeout,
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out.strip()
  except Exception as e:
    return -1, f"{type(e).__name__}: {e}"


def _tmux_boot_trace() -> tuple[str, list[str]]:
  rc, trace = _run_cmd(["tmux", "capture-pane", "-p", "-S", "-1000", "-t", "comma"], timeout=3.0)
  if rc != 0:
    rc, trace = _run_cmd(["tmux", "capture-pane", "-pq", "-S-1000"], timeout=3.0)
  if rc != 0:
    return trace, []

  lines = trace.splitlines()
  picked: list[str] = []
  for index, line in enumerate(lines):
    low = line.lower()
    if "traceback (most recent call last):" in low:
      picked.extend(lines[index:min(index + 16, len(lines))])
      continue
    if any(token in low for token in BOOT_ERROR_TOKENS[1:]):
      picked.append(line)

  deduped: list[str] = []
  for line in picked:
    line = line.rstrip()
    if line and (not deduped or deduped[-1] != line):
      deduped.append(line)
  return "", deduped[-40:]


def _read_uptime_seconds() -> float | None:
  try:
    with open("/proc/uptime", "r", encoding="utf-8") as src:
      return float(src.read().split()[0])
  except Exception:
    return None


def _read_boot_params() -> tuple[bool | None, bool | None, bool | None, str]:
  try:
    from openpilot.common.params import Params

    params = Params()
    controls_ready = params.get_bool("ControlsReady")
    firmware_done = params.get_bool("FirmwareQueryDone")
    carparams_saved = params.get("CarParams") is not None
    return controls_ready, firmware_done, carparams_saved, ""
  except Exception as e:
    return None, None, None, f"{type(e).__name__}: {e}"


def _process_snapshot() -> tuple[dict[str, bool], list[str]]:
  states: dict[str, bool] = {}
  rows: list[str] = []
  for name in BOOT_CRITICAL_PROCS:
    rc, out = _run_cmd(["pgrep", "-af", name], timeout=1.0)
    active = rc == 0 and bool(out.strip())
    states[name] = active
    if active:
      first = out.splitlines()[0].strip()
      rows.append(f"{name}: running | {first[:180]}")
    else:
      rows.append(f"{name}: not running")
  return states, rows


def print_boot_diagnostic() -> None:
  """Append a read-only boot/startup health section to the integrated report."""
  print("")
  print("[29] 부팅 · manager · 핵심 프로세스 진단")
  print("※ 읽기 전용입니다. 재부팅·프로세스 재시작·Params 변경·CAN 송신은 하지 않습니다.")

  uptime = _read_uptime_seconds()
  if uptime is None:
    print("장치 uptime: 확인 실패")
  else:
    print(f"장치 uptime: {uptime:.1f}초 ({uptime / 60.0:.1f}분)")

  controls_ready, firmware_done, carparams_saved, param_error = _read_boot_params()
  if param_error:
    print(f"Params 확인 실패: {param_error}")
  else:
    print(
      f"ControlsReady={b(controls_ready)} | FirmwareQueryDone={b(firmware_done)} | "
      f"CarParams 저장={b(carparams_saved)}"
    )

  states, process_rows = _process_snapshot()
  print("프로세스 상태:")
  for row in process_rows:
    print("  " + row)

  trace_error, boot_errors = _tmux_boot_trace()
  if trace_error:
    print(f"tmux 부팅 로그 확인 실패: {trace_error}")
  elif boot_errors:
    print("최근 부팅/실행 오류 후보:")
    for row in boot_errors:
      print("  " + row)
  else:
    print("최근 tmux에서 명확한 Python/치명적 부팅 오류 문자열 없음")

  manager_alive = bool(states.get("manager"))
  ui_alive = bool(states.get("ui"))
  if not manager_alive:
    print("[부팅 실패 후보] manager 프로세스가 보이지 않습니다. launch/manager 시작 단계부터 확인이 필요합니다.")
  elif boot_errors and controls_ready is False:
    print("[부팅 실패 후보] manager는 실행 중이지만 제어 스택 준비 전 오류가 확인됩니다. 위 traceback 마지막 부분을 우선 확인하십시오.")
  elif manager_alive and ui_alive and controls_ready is True:
    print("[부팅 정상 후보] manager · UI · ControlsReady가 확인됩니다.")
  elif manager_alive and controls_ready is False:
    print("[부팅 지연/부분 시작] manager는 살아 있으나 ControlsReady=False입니다. 차량 offroad 상태이거나 초기화가 완료되지 않은 상태일 수 있습니다.")
  else:
    print("[부팅 부분 확인] manager는 실행 중입니다. 위 프로세스와 tmux 오류를 함께 확인하십시오.")


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
    print_boot_diagnostic()
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
  print_boot_diagnostic()
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("[28] Jetson Orin · YOLO 연결 진단")
    print(f"Jetson 진단 내부 오류: {type(e).__name__}: {e}")
    print("※ 추가 진단 오류가 전체 8초 진단 다운로드를 막지 않도록 종료코드는 0으로 반환합니다.")
    try:
      print_boot_diagnostic()
    except Exception as boot_error:
      print("[29] 부팅 · manager · 핵심 프로세스 진단")
      print(f"부팅 진단 내부 오류: {type(boot_error).__name__}: {boot_error}")
    raise SystemExit(0)
