#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import nexo_can_diag_download_base as base

JETLINK = "/data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_jetlink_diag.py"
JETLINK_STATUS = "/dev/shm/nexo-jetlink.json"
JETLINK_GADGET = "/sys/kernel/config/usb_gadget/jetlink"
JETLINK_FFS = "/dev/ffs-jetlink"
JETLINK_ENABLE = "/data/nexo_jetlink_enabled"


def _jetlink_report() -> str:
  try:
    proc = subprocess.run(
      [sys.executable, JETLINK],
      cwd="/data/openpilot",
      capture_output=True,
      text=True,
      timeout=5.0,
    )
    text = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
    if text:
      return text
    return f"[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단\nJetlink 추가 진단 출력 없음 exit_code={proc.returncode}"
  except Exception as e:
    return (
      "[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단\n"
      f"Jetlink 추가 진단 실패: {type(e).__name__}: {e}\n"
      "※ Jetlink 추가 진단 실패는 기존 8초 통합진단 완료 여부에 영향을 주지 않습니다."
    )


def _proc_running(name: str) -> bool:
  try:
    proc = subprocess.run(
      ["pgrep", "-x", name],
      cwd="/data/openpilot",
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      timeout=1.0,
    )
    return proc.returncode == 0
  except Exception:
    return False


def _read_json(path: str) -> dict:
  try:
    with open(path, "r", encoding="utf-8") as src:
      value = json.load(src)
    return value if isinstance(value, dict) else {}
  except Exception:
    return {}


def _read_text(path: str) -> str:
  try:
    with open(path, "r", encoding="utf-8", errors="replace") as src:
      return src.read().strip()
  except Exception:
    return ""


def _yn(value: object) -> str:
  if value is None:
    return "Unknown"
  return "True" if bool(value) else "False"


def _vehicle_connection_report() -> str:
  """Classify whether this report was captured under useful real-vehicle Jetlink conditions.

  Observation only: this function subscribes to existing cereal services and reads
  process/configfs/status files. It never changes Params, CAN, Panda, USB ownership,
  Jetlink state, or vehicle control.
  """
  controls_ready: bool | None = None
  device_started: bool | None = None
  car_seen = False
  car_valid: bool | None = None
  panda_seen = False
  ignition_line: bool | None = None
  ignition_can: bool | None = None
  road_seen = False
  road_valid: bool | None = None
  probe_error = ""

  try:
    from openpilot.cereal import messaging
    from openpilot.common.params import Params

    controls_ready = Params().get_bool("ControlsReady")
    sm = messaging.SubMaster(["deviceState", "carState", "pandaStates", "roadEncodeData"])
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
      sm.update(100)

      if sm.seen["deviceState"]:
        try:
          device_started = bool(sm["deviceState"].started)
        except Exception:
          pass

      if sm.seen["carState"]:
        car_seen = True
        try:
          car_valid = bool(sm.valid["carState"])
        except Exception:
          pass

      if sm.seen["pandaStates"]:
        panda_seen = True
        try:
          panda_states = sm["pandaStates"]
          if len(panda_states) > 0:
            ignition_line = any(bool(p.ignitionLine) for p in panda_states)
            ignition_can = any(bool(p.ignitionCan) for p in panda_states)
        except Exception:
          pass

      if sm.seen["roadEncodeData"]:
        road_seen = True
        try:
          road_valid = bool(sm.valid["roadEncodeData"])
        except Exception:
          pass
  except Exception as e:
    probe_error = f"{type(e).__name__}: {e}"

  camerad = _proc_running("camerad")
  encoderd = _proc_running("encoderd")
  jetlink_enabled = os.path.exists(JETLINK_ENABLE)
  configfs = os.path.isdir(JETLINK_GADGET)
  ffs_mount = os.path.isdir(JETLINK_FFS) and os.path.exists(os.path.join(JETLINK_FFS, "ep0"))
  udc = _read_text(os.path.join(JETLINK_GADGET, "UDC")) if configfs else ""
  udc_bound = bool(udc)
  jetlink = _read_json(JETLINK_STATUS)
  jetlink_state = str(jetlink.get("state") or "unknown")
  jetlink_error = str(jetlink.get("error") or "").strip()

  # deviceState.started + a valid carState is the most useful read-only proof
  # that this capture actually came from an onroad/vehicle-connected session.
  vehicle_sample = bool(device_started is True and car_seen and car_valid is True)
  ignition_on = None if not panda_seen else bool(ignition_line or ignition_can)
  camera_sample = bool(camerad and encoderd and road_seen and road_valid is not False)
  # Dedicated FunctionFS and shared g1 NCM have different readiness rules.
  # Inspect only the bound NCM function; usb0 by name alone is not evidence.
  transport_mode = "unknown"
  ncm_link = {}
  try:
    from openpilot.selfdrive.modeld.jetlink.ncm_transport import transport_mode as _selected_transport
    from openpilot.selfdrive.carrot.server.services.jetson_diagnostics import _ncm_observation
    transport_mode = _selected_transport()
    ncm_link = _ncm_observation()
  except (OSError, ValueError, RuntimeError, ImportError) as error:
    probe_error += f" | NCM 확인 불가: {type(error).__name__}"
  if transport_mode == "ncm-tcp":
    usb_ready = bool(ncm_link.get("udc_bound") is True and
                     len(ncm_link.get("ncm_interfaces") or []) == 1 and
                     ncm_link.get("ncm_admin_up") is True and
                     ncm_link.get("ncm_carrier") is True and
                     ncm_link.get("comma_ipv4") == ncm_link.get("expected_comma_ipv4") and
                     ncm_link.get("peer_route_via_usb") is True)
  elif transport_mode == "usb":
    usb_ready = bool(configfs and ffs_mount and udc_bound)
  else:
    usb_ready = False

  lines = [
    "[32] 실차 연결 조건 · Jetson 진단 신뢰도",
    "※ 읽기 전용입니다. 차량 제어·CAN·Panda·Params·USB 설정을 변경하지 않습니다.",
    (
      "차량 샘플: "
      f"deviceState.started={_yn(device_started)} | carState seen={car_seen} | "
      f"valid={_yn(car_valid)} | ControlsReady={_yn(controls_ready)}"
    ),
    (
      "시동/READY 보조 확인: "
      f"pandaStates seen={panda_seen} | ignitionLine={_yn(ignition_line)} | "
      f"ignitionCan={_yn(ignition_can)} | ignition_on={_yn(ignition_on)}"
    ),
    (
      "콤마 영상 조건: "
      f"camerad={camerad} | encoderd={encoderd} | roadEncodeData seen={road_seen} | "
      f"valid={_yn(road_valid)}"
    ),
    (
      "Jetlink USB 조건: "
      f"transport={transport_mode} | enabled={jetlink_enabled} | "
      f"dedicated_configfs={configfs} | ffs_mount={ffs_mount} | "
      f"dedicated_UDC_bound={udc_bound} | UDC={udc or '-'} | state={jetlink_state}"
    ),
    (
      "AGNOS g1 NCM (읽기 전용): "
      f"bound={_yn(ncm_link.get('udc_bound'))} | iface={ncm_link.get('ncm_interface') or '-'} | "
      f"admin={_yn(ncm_link.get('ncm_admin_up'))} | "
      f"carrier={_yn(ncm_link.get('ncm_carrier'))} | "
      f"comma_IP={ncm_link.get('comma_ipv4') or '-'} | "
      f"usb_route={_yn(ncm_link.get('peer_route_via_usb'))} | "
      f"neighbor={ncm_link.get('neighbor_cache_state') or '-'}"
    ),
  ]

  if probe_error:
    lines.append(f"실차 조건 관측 참고: {probe_error}")

  if not vehicle_sample:
    lines.append(
      "[검사 조건 미충족] 차량 READY/시동 ON 상태의 실차 샘플이 확인되지 않았습니다. "
      "이 상태의 CAMERA/JETSON DISCONNECTED 결과는 참고용이며 차량 READY + Jetson 전원 + USB 연결 상태에서 다시 실행하십시오."
    )
  elif not camera_sample:
    lines.append(
      "[차량 연결 확인 · 영상 조건 미충족] 실차 연결은 확인됐지만 콤마 영상 경로가 완전히 확인되지 않았습니다. "
      "camerad/encoderd/roadEncodeData 항목을 우선 확인하십시오."
    )
  elif jetlink_enabled and not usb_ready:
    lines.append(
      "[실차 조건 충족 · Jetlink USB 문제] 차량과 콤마 영상은 확인됐지만 Jetlink USB gadget이 준비되지 않았습니다. "
      + ("기존 AGNOS g1 NCM 바인딩·carrier·192.168.100.127/24·192.168.100.250 전용 라우트를 확인하십시오."
         if transport_mode == "ncm-tcp" else
         "전용 USB FunctionFS/configfs/UDC 구성을 확인하십시오. 케이블 고장은 확정할 수 없습니다.")
    )
  elif jetlink_enabled and jetlink_state != "ready":
    lines.append(
      "[실차 조건 충족 · Jetson 연결 대기] 차량·영상·USB 기본 조건은 확인됐지만 Jetlink가 ready 상태가 아닙니다. "
      "Jetson 전원·USB host 인식·Jetlink 서버/heartbeat를 확인하십시오."
    )
  elif jetlink_enabled and jetlink_state == "ready":
    lines.append("[Jetlink ready 보고] 차량·영상과 설정 경로는 준비됐지만 HELLO·최근 추론 응답·실제 HUD 출력은 각각 추가 확인해야 합니다.")
  else:
    lines.append("[실차 조건 충족 · Jetlink 비활성] 차량과 영상 조건은 확인됐지만 Jetlink 기능이 꺼져 있습니다.")

  if jetlink_error:
    lines.append(f"Jetlink 현재 오류: {jetlink_error[:400]}")
  if not jetlink_enabled:
    lines.append("※ 추론 OFF는 오류가 아닙니다. USB carrier 유무를 원격 서버 또는 실제 HUD 동작으로 해석하지 마십시오.")
  if transport_mode == "ncm-tcp" and usb_ready:
    lines.append("※ NCM 통신 경로 확인은 TCP/HELLO 응답이나 TensorRT/YOLO 추론 성공의 증거가 아닙니다.")

  return "\n".join(lines)


def _insert_before_completion(tmp_path: str, section: str) -> None:
  with open(tmp_path, "r", encoding="utf-8", errors="replace") as src:
    lines = src.readlines()

  marker_index = None
  for index in range(len(lines) - 1, -1, -1):
    if lines[index].startswith("NEXO_DIAG_COMPLETE") or lines[index].startswith("NEXO_DIAG_FAILED"):
      marker_index = index
      break

  section_text = "\n\n" + section.rstrip() + "\n"
  if marker_index is None:
    text = "".join(lines).rstrip() + section_text
  else:
    text = "".join(lines[:marker_index]).rstrip() + section_text + "\n" + "".join(lines[marker_index:])

  with open(tmp_path, "w", encoding="utf-8") as report:
    report.write(text)
    report.flush()
    os.fsync(report.fileno())


def worker(tmp_path: str) -> int:
  """Run preserved 8-second collectors and append read-only Jetlink/context sections."""
  patched_diag = None
  patched_forensic = None
  try:
    warmup = base._wait_for_control_stack()
    patched_diag = base._make_compatible_diag(tmp_path)
    patched_forensic = base._make_compatible_forensic(tmp_path)
    core_rc, timeline_rc = base._run_parallel(patched_diag, patched_forensic, tmp_path, warmup)

    # Add Jetlink only after all existing collectors are complete, but before
    # the report is atomically published. This keeps the old diagnostic logic
    # untouched and prevents partial add-on sections from being downloaded.
    try:
      _insert_before_completion(tmp_path, _jetlink_report())
    except Exception as e:
      try:
        _insert_before_completion(
          tmp_path,
          "[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단\n"
          f"Jetlink 결과 병합 실패: {type(e).__name__}: {e}\n"
          "※ 기존 8초 통합진단 결과는 그대로 유지합니다.",
        )
      except Exception:
        pass

    # Make the TXT self-describing: it now says whether the capture itself was
    # taken under real-vehicle/onroad conditions before interpreting Jetson faults.
    try:
      _insert_before_completion(tmp_path, _vehicle_connection_report())
    except Exception as e:
      try:
        _insert_before_completion(
          tmp_path,
          "[32] 실차 연결 조건 · Jetson 진단 신뢰도\n"
          f"실차 조건 추가 진단 실패: {type(e).__name__}: {e}\n"
          "※ 기존 8초 통합진단 및 Jetlink 결과는 그대로 유지합니다.",
        )
      except Exception:
        pass

    # Read only the completed TXT: never submit vehicle control or USB actions.
    try:
      from openpilot.selfdrive.carrot.server.features.tools.nexo_diag_quality import interpretation
      with open(tmp_path, "r", encoding="utf-8", errors="replace") as summary_source:
        collected = summary_source.read(2_000_001)
      if len(collected) <= 2_000_000:
        _insert_before_completion(tmp_path, interpretation(collected))
      else:
        _insert_before_completion(tmp_path, "[33] 진단 신뢰도\\n자료가 너무 커서 자동 판정을 생략했습니다.".replace("\\n", "\n"))
    except Exception as e:
      try:
        _insert_before_completion(
          tmp_path,
          "[33] 진단 신뢰도\\n"
          f"자동 판정 생략: {type(e).__name__}. 기존 원시 진단 기록을 확인하십시오.".replace("\\n", "\n"),
        )
      except Exception:
        pass

    os.replace(tmp_path, base.REPORT)
    return 0 if core_rc == 0 and timeline_rc == 0 else 1
  except Exception as e:
    base._publish_failure(tmp_path, f"{type(e).__name__}: {e}")
    return 1
  finally:
    for patched_path in (patched_diag, patched_forensic):
      if patched_path:
        try:
          os.remove(patched_path)
        except Exception:
          pass


def main() -> int:
  try:
    os.makedirs(os.path.dirname(base.REPORT), exist_ok=True)
    try:
      os.remove(base.REPORT)
    except FileNotFoundError:
      pass

    token = f"{os.getpid()}-{time.time_ns()}"
    tmp_path = f"{base.REPORT}.{token}.tmp"
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
