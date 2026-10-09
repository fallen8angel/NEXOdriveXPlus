#!/usr/bin/env python3
from __future__ import annotations

import json
import re
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
  if value is None:
    return "Unknown"
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


def _read_reboot_request() -> tuple[bool | None, str]:
  try:
    from openpilot.common.params import Params

    return Params().get_bool("DoReboot"), ""
  except Exception as e:
    return None, f"{type(e).__name__}: {e}"


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


def _proc_info(pattern: str) -> tuple[bool, str]:
  rc, out = _run_cmd(["pgrep", "-af", pattern], timeout=1.0)
  if rc != 0 or not out.strip():
    return False, ""
  return True, out.splitlines()[0].strip()


def _bridge_port(endpoint: str) -> int:
  # Must match cereal/messaging/bridge_zmq.cc get_port() on 64-bit comma devices.
  hash_value = 0xCBF29CE484222325
  fnv_prime = 0x100000001B3
  for value in endpoint.encode("utf-8"):
    hash_value ^= value
    hash_value = (hash_value * fnv_prime) & 0xFFFFFFFFFFFFFFFF
  start_port = 8023
  max_port = 65535
  return start_port + (hash_value % (max_port - start_port))


def _tcp_port_listening(port: int) -> tuple[bool | None, str]:
  rc, out = _run_cmd(["ss", "-Hltn"], timeout=1.5)
  if rc == 0:
    for line in out.splitlines():
      if re.search(rf":{port}(?:\s|$)", line):
        return True, "ss"
    return False, "ss"

  target = f"{port:04X}".upper()
  try:
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
      with open(path, "r", encoding="utf-8") as src:
        for line in src.readlines()[1:]:
          cols = line.split()
          if len(cols) < 4:
            continue
          local_addr = cols[1]
          state = cols[3]
          if local_addr.rsplit(":", 1)[-1].upper() == target and state == "0A":
            return True, "proc"
    return False, "proc"
  except Exception as e:
    return None, f"{type(e).__name__}: {e}"


def _bridge_server_info() -> tuple[bool, str]:
  """Find the manager-owned NEXO bridge regardless of its executable path.

  The manager launches it as `./bridge --publish roadEncodeData`, so checking
  only for the source-tree string `cereal/messaging/bridge` gives a false
  negative even when the publisher is actually listening.
  """
  rc, out = _run_cmd(["ps", "-eo", "pid=,args="], timeout=1.5)
  if rc != 0 or not out.strip():
    return False, ""

  candidates: list[str] = []
  for line in out.splitlines():
    stripped = line.strip()
    parts = stripped.split(maxsplit=1)
    command = parts[1] if len(parts) == 2 else ""
    if not command:
      continue
    if "--publish" not in command or "roadEncodeData" not in command:
      continue
    if not re.search(r"(?:^|[/\s])bridge(?:\s|$)", command):
      continue
    candidates.append(stripped)

  return (bool(candidates), candidates[0] if candidates else "")


def _init_local_stream_probe():
  try:
    from openpilot.cereal import messaging

    sm = messaging.SubMaster(["roadEncodeData", "deviceState"], poll="roadEncodeData")
    return sm, ""
  except Exception as e:
    return None, f"{type(e).__name__}: {e}"


def _sample_local_stream(sm, state: dict) -> None:
  if sm is None:
    return
  try:
    sm.update(0)
    if sm.updated["deviceState"]:
      try:
        state["device_started"] = bool(sm["deviceState"].started)
      except Exception:
        pass
    if sm.updated["roadEncodeData"]:
      state["road_seen"] = True
      state["road_updates"] += 1
      try:
        state["road_valid"] = bool(sm.valid["roadEncodeData"])
      except Exception:
        pass
      try:
        state["road_encode_id"] = int(sm["roadEncodeData"].encodeId)
      except Exception:
        pass
  except Exception as e:
    if not state["probe_error"]:
      state["probe_error"] = f"{type(e).__name__}: {e}"


def _local_camera_snapshot(stream_state: dict) -> dict:
  camerad, camerad_cmd = _proc_info("camerad")
  encoderd, encoderd_cmd = _proc_info("encoderd")
  bridge_server, bridge_cmd = _bridge_server_info()
  road_port = _bridge_port("roadEncodeData")
  road_port_listening, listen_source = _tcp_port_listening(road_port)

  return {
    **stream_state,
    "camerad": camerad,
    "camerad_cmd": camerad_cmd,
    "encoderd": encoderd,
    "encoderd_cmd": encoderd_cmd,
    "bridge_server": bridge_server,
    "bridge_cmd": bridge_cmd,
    "road_port": road_port,
    "road_port_listening": road_port_listening,
    "listen_source": listen_source,
  }


def _print_local_camera_snapshot(local: dict) -> None:
  print(
    "콤마 영상원본: "
    f"device_started={b(local.get('device_started'))} | "
    f"camerad={b(local.get('camerad'))} | encoderd={b(local.get('encoderd'))}"
  )
  print(
    "roadEncodeData(local): "
    f"seen={b(local.get('road_seen'))} | updates={local.get('road_updates', 0)} | "
    f"valid={b(local.get('road_valid'))} | encodeId={local.get('road_encode_id', '-')}"
  )
  print(
    "콤마 ZMQ export: "
    f"bridge_server={b(local.get('bridge_server'))} | "
    f"roadEncodeData_port={local.get('road_port', '-')} | "
    f"listening={b(local.get('road_port_listening'))}"
  )
  if local.get("bridge_cmd"):
    print(f"bridge process: {str(local['bridge_cmd'])[:220]}")
  elif local.get("road_port_listening") is True:
    print("bridge process note: 실행 문자열은 미검출됐지만 ZMQ 포트가 LISTEN 중이므로 export 서버는 열린 상태로 봅니다.")
  if local.get("probe_error"):
    print(f"local stream probe note: {local['probe_error']}")
  if local.get("road_port_listening") is None:
    print(f"port probe note: {local.get('listen_source', '-')}")


def _print_root_cause_hint(local: dict, heartbeat_present: bool, comma_tcp: bool, frame_seen: bool, yolo_recent: bool) -> None:
  device_started = local.get("device_started")
  camerad = bool(local.get("camerad"))
  encoderd = bool(local.get("encoderd"))
  road_seen = bool(local.get("road_seen"))
  bridge_server = bool(local.get("bridge_server"))
  road_port_listening = local.get("road_port_listening")

  if not heartbeat_present:
    print("[원인 분기] Jetson heartbeat 자체가 없어 Jetson 전원·네트워크·status beacon부터 확인해야 합니다.")
  elif device_started is False:
    print("[원인 분기] 콤마 deviceState.started=False입니다. 차량이 onroad가 아니면 roadEncodeData가 없을 수 있습니다.")
  elif not camerad or not encoderd:
    print("[원인 분기] 콤마 camerad/encoderd 중 일부가 실행되지 않습니다. Jetson보다 콤마 영상 생성 단계를 먼저 확인해야 합니다.")
  elif not road_seen:
    print("[원인 분기] camerad/encoderd는 보이지만 8초 동안 local roadEncodeData를 직접 수신하지 못했습니다.")
  elif road_port_listening is False:
    print("[원인 분기] 콤마 local roadEncodeData는 있으나 roadEncodeData ZMQ 포트가 LISTEN 상태가 아닙니다.")
    print("확인: manager의 nexo_jetson_bridge 실행 여부와 roadEncodeData ZMQ publisher를 확인하십시오.")
  elif not comma_tcp:
    if not bridge_server and road_port_listening is True:
      print("[진단 보정] bridge 프로세스 문자열은 직접 잡히지 않았지만 ZMQ 포트가 LISTEN 중입니다. export는 열린 것으로 판단합니다.")
    print("[원인 분기] 콤마 영상·ZMQ export는 정상인데 Jetson comma_tcp=False입니다. Jetson bridge 대상 IP·TCP 경로·방화벽 쪽 후보입니다.")
  elif not frame_seen:
    if not bridge_server and road_port_listening is True:
      print("[진단 보정] ZMQ 포트가 LISTEN 중이고 Jetson comma_tcp=True이므로 bridge 서버 미검출값은 경로 표기 차이로 판단합니다.")
    print("[원인 분기] TCP 연결은 됐지만 Jetson에서 첫 I-frame 확인이 없습니다. frame bridge/영상 전달 단계를 확인하십시오.")
  elif not yolo_recent:
    print("[원인 분기] 영상 프레임은 Jetson에 도착했지만 최근 YOLO 추론이 없습니다. YOLO worker/model 실행 단계를 확인하십시오.")
  else:
    print("[원인 분기] 콤마 영상 생성 → ZMQ export → Jetson TCP → frame → YOLO 전체 경로가 정상입니다.")


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


def _git_text(args: list[str]) -> str:
  rc, out = _run_cmd(["git", *args], timeout=2.0)
  return out.strip() if rc == 0 else ""


def _sanitize_remote_url(value: str) -> str:
  return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", str(value or ""))


def _dispatcher_capabilities() -> tuple[bool, bool, str]:
  path = f"{REPO_ROOT}/openpilot/selfdrive/carrot/server/features/tools/dispatcher.py"
  try:
    with open(path, "r", encoding="utf-8") as src:
      text = src.read()
    git_pull = 'action == "git_pull"' in text or "action == 'git_pull'" in text
    reboot = 'action == "reboot"' in text or "action == 'reboot'" in text
    return git_pull, reboot, ""
  except Exception as e:
    return False, False, f"{type(e).__name__}: {e}"


def print_remote_update_diagnostic() -> None:
  """Read-only checks for remote web update and reboot readiness."""
  print("")
  print("[30] 원격 업데이트 · 재부팅 준비 진단")
  print("※ 읽기 전용입니다. git pull·reset·재부팅을 실행하지 않습니다.")

  branch = _git_text(["branch", "--show-current"]) or "-"
  head = _git_text(["rev-parse", "--short=12", "HEAD"]) or "-"
  origin = _sanitize_remote_url(_git_text(["remote", "get-url", "origin"])) or "-"
  upstream = _git_text(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]) or "-"
  dirty_rows = _git_text(["status", "--porcelain"])
  dirty_count = len([line for line in dirty_rows.splitlines() if line.strip()])

  carrot_server, carrot_server_cmd = _proc_info("carrot_server")
  web_listening, web_source = _tcp_port_listening(7000)
  git_pull_cap, reboot_cap, capability_error = _dispatcher_capabilities()
  do_reboot, reboot_error = _read_reboot_request()

  print(f"git: branch={branch} | head={head} | upstream={upstream}")
  print(f"origin={origin}")
  print(f"working tree: dirty={dirty_count > 0} | changed_files={dirty_count}")
  print(
    f"7000 server: process={b(carrot_server)} | port_listening={b(web_listening)} | "
    f"probe={web_source or '-'}"
  )
  if carrot_server_cmd:
    print(f"carrot_server process: {carrot_server_cmd[:220]}")
  print(f"remote actions: git_pull={b(git_pull_cap)} | reboot={b(reboot_cap)} | DoReboot={b(do_reboot)}")
  if capability_error:
    print(f"dispatcher 확인 note: {capability_error}")
  if reboot_error:
    print(f"DoReboot 확인 note: {reboot_error}")

  if branch != "-" and origin != "-" and web_listening is True and git_pull_cap and reboot_cap:
    print("[원격 업데이트 준비] 7000 서버에서 업데이트 확인·적용·재부팅 요청 경로를 사용할 수 있습니다.")
  else:
    print("[원격 업데이트 점검 필요] 위 git/7000/action 항목 중 False 또는 '-'인 항목을 먼저 확인하십시오.")

  if dirty_count > 0:
    print("[주의] 로컬 수정 파일이 있습니다. 웹 git_pull은 먼저 git reset --hard를 수행하므로 저장하지 않은 차량 내 수정은 사라질 수 있습니다.")
  if do_reboot is True:
    print("[재부팅 요청 대기] DoReboot=True입니다. manager가 요청을 처리하면 장치가 재부팅됩니다.")
  elif do_reboot is False:
    print("[재부팅 요청 없음] 현재 DoReboot=False입니다.")


def main() -> int:
  started = time.monotonic()
  deadline = started + OBSERVE_SECONDS
  packets = 0
  last = None
  last_src = "-"
  error = ""

  local_sm, local_probe_error = _init_local_stream_probe()
  stream_state = {
    "device_started": None,
    "road_seen": False,
    "road_updates": 0,
    "road_valid": None,
    "road_encode_id": None,
    "probe_error": local_probe_error,
  }

  sock = None
  receiver_conflict = False
  receiver_state = None
  try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", STATUS_PORT))
    sock.settimeout(0.35)

    while time.monotonic() < deadline:
      _sample_local_stream(local_sm, stream_state)
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

    _sample_local_stream(local_sm, stream_state)
  except OSError as e:
    error = f"{type(e).__name__}: {e}"
    if e.errno == 98:
      receiver_conflict = True
      # The 7000 web service owns the heartbeat port. Query its read-only
      # status endpoint instead of trying to steal or share UDP datagrams.
      try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:7000/api/jetson/status", timeout=1.5) as response:
          receiver_state = json.load(response)
        if receiver_state.get("connected") and isinstance(receiver_state.get("status"), dict):
          last = receiver_state["status"]
          packets = 1
          last_src = str(last.get("ip", "-"))
          error = ""
      except Exception as probe_error:
        error += f" | web status unavailable: {type(probe_error).__name__}: {probe_error}"
  except Exception as e:
    error = f"{type(e).__name__}: {e}"
  finally:
    if sock is not None:
      try:
        sock.close()
      except Exception:
        pass

  local = _local_camera_snapshot(stream_state)

  print("[28] Jetson Orin · YOLO 연결 진단")
  print("※ 읽기 전용 상태 확인입니다. 차량 제어/CAN/Panda에는 명령을 보내지 않습니다.")
  _print_local_camera_snapshot(local)

  if last is None:
    print(f"heartbeat: 0 packets / {OBSERVE_SECONDS:.0f}s")
    if error:
      print(f"listener error: {error}")
    print("[진단 불가] heartbeat 포트가 이미 사용 중입니다. 7000 서버 수신기 상태를 확인하십시오." if receiver_conflict else "[미수신] Jetson 상태 heartbeat를 받지 못했습니다.")
    print("확인: Jetson 전원 · 같은 Wi-Fi/핫스팟 · nexo-yolo.service · jetson_status_beacon.py")
    if not receiver_conflict:
      _print_root_cause_hint(local, False, False, False, False)
    print_boot_diagnostic()
    print_remote_update_diagnostic()
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
    f"Jetson process: bridge={b(bridge_proc)} | frame_bridge={b(frame_bridge_proc)} | "
    f"yolo_worker={b(yolo_proc)} | local_pipe_8765={b(local_pipe)}"
  )
  print(f"Jetson frame: camera_frame_seen={b(frame_seen)} | yolo_recent={b(yolo_recent)}")

  if last.get("last_encode_id") is not None:
    print(
      f"YOLO last: encodeId={last.get('last_encode_id')} | det={last.get('last_det','-')} | "
      f"infer={last.get('last_infer_ms','-')}ms | age={last.get('yolo_age_ms','-')}ms"
    )

  if bridge_proc and frame_bridge_proc and yolo_proc and local_pipe and comma_tcp and yolo_recent:
    print("[정상] Jetson · 콤마 영상 링크 · YOLO 추론이 모두 동작 중입니다.")
  elif bridge_proc and frame_bridge_proc and yolo_proc and local_pipe and not comma_tcp:
    print("[대기] Jetson 내부 파이프라인은 정상이나 콤마 roadEncodeData TCP 링크가 연결되지 않았습니다.")
  elif bridge_proc and frame_bridge_proc and yolo_proc and local_pipe and comma_tcp and not yolo_recent:
    print("[연결됨·영상대기] 콤마 TCP 링크는 있으나 최근 YOLO 추론 로그가 없습니다.")
  else:
    print("[주의] Jetson heartbeat는 수신됐지만 일부 프로세스/연결이 준비되지 않았습니다.")

  _print_root_cause_hint(local, True, comma_tcp, frame_seen, yolo_recent)

  if error:
    print(f"listener note: {error}")
  print_boot_diagnostic()
  print_remote_update_diagnostic()
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
    try:
      print_remote_update_diagnostic()
    except Exception as update_error:
      print("[30] 원격 업데이트 · 재부팅 준비 진단")
      print(f"원격 업데이트 진단 내부 오류: {type(update_error).__name__}: {update_error}")
    raise SystemExit(0)
