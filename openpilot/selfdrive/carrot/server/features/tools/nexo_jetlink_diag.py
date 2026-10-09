#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

ENABLE_FILE = Path("/data/nexo_jetlink_enabled")
STATUS = Path("/dev/shm/nexo-jetlink.json")
MODEL_STATUS = Path("/dev/shm/nexo-jetlink-model.json")
SPEC_FILE = Path("/dev/shm/nexo-jetlink-spec.json")
FAULT = Path("/dev/shm/nexo-jetlink-fault")
SOCKET = Path("/dev/shm/nexo-jetlink.sock")
GADGET = Path("/sys/kernel/config/usb_gadget/jetlink")
FFS_MOUNT = Path("/dev/ffs-jetlink")
NATIVE_MODEL = Path("/data/openpilot/openpilot/selfdrive/modeld/models/driving_supercombo.onnx")
DAEMON_PATTERN = "openpilot.selfdrive.modeld.jetlink.daemon"


def b(value: object) -> str:
  if value is None:
    return "Unknown"
  return "True" if bool(value) else "False"


def _read_json(path: Path) -> tuple[dict, str]:
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
      return {}, "JSON root is not an object"
    return value, ""
  except FileNotFoundError:
    return {}, ""
  except Exception as e:
    return {}, f"{type(e).__name__}: {e}"


def _mono_age(value: object) -> float | None:
  try:
    age = time.monotonic() - float(value)
    return max(0.0, age)
  except Exception:
    return None


def _file_age(path: Path) -> float | None:
  try:
    return max(0.0, time.time() - path.stat().st_mtime)
  except Exception:
    return None


def _process() -> tuple[bool, str]:
  try:
    proc = subprocess.run(
      ["pgrep", "-af", DAEMON_PATTERN],
      capture_output=True,
      text=True,
      timeout=1.5,
    )
    out = (proc.stdout or "").strip()
    return proc.returncode == 0 and bool(out), out.splitlines()[0].strip() if out else ""
  except Exception as e:
    return False, f"{type(e).__name__}: {e}"


def _display_usb_enabled() -> tuple[bool | None, str]:
  try:
    from openpilot.common.params import Params

    return Params().get_bool("NexoJetsonUsb"), ""
  except Exception as e:
    return None, f"{type(e).__name__}: {e}"


def _udc_name() -> str:
  try:
    return (GADGET / "UDC").read_text(encoding="utf-8").strip()
  except Exception:
    return ""


def _short_sha(value: object) -> str:
  text = str(value or "")
  return text[:16] + ("…" if len(text) > 16 else "")


def _compact(value: object, limit: int = 260) -> str:
  try:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
  except Exception:
    text = str(value)
  return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
  enabled = ENABLE_FILE.exists()
  daemon, daemon_cmd = _process()
  display_usb, display_error = _display_usb_enabled()

  usb_status, usb_error = _read_json(STATUS)
  model_status, model_error = _read_json(MODEL_STATUS)
  spec, spec_error = _read_json(SPEC_FILE)

  usb_state = str(usb_status.get("state", "-"))
  usb_age = _mono_age(usb_status.get("updated"))
  model_age = _mono_age(model_status.get("updated"))
  usb_fresh = usb_age is not None and usb_age <= 5.0
  model_fresh = model_age is not None and model_age <= 5.0

  spec_sha = str(spec.get("sha256", ""))
  usb_sha = str(usb_status.get("sha256", ""))
  model_sha = str(model_status.get("sha256", ""))
  usb_contract_match = bool(spec_sha and usb_sha and spec_sha == usb_sha)
  model_contract_match = bool(spec_sha and model_sha and spec_sha == model_sha)

  native_exists = NATIVE_MODEL.is_file()
  try:
    native_nbytes = NATIVE_MODEL.stat().st_size if native_exists else None
  except Exception:
    native_nbytes = None
  try:
    spec_nbytes = int(spec.get("nbytes")) if spec.get("nbytes") is not None else None
  except Exception:
    spec_nbytes = None
  native_size_match = native_nbytes is not None and spec_nbytes is not None and native_nbytes == spec_nbytes

  fault_age = _file_age(FAULT)
  recent_fault = fault_age is not None and fault_age <= 120.0
  gadget_exists = GADGET.is_dir()
  ffs_exists = FFS_MOUNT.is_dir()
  socket_exists = SOCKET.exists()
  udc = _udc_name()

  print("[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단")
  print("※ 읽기 전용입니다. Jetlink 활성화·USB 설정·모델 전환·CAN/Panda/차량 제어를 변경하지 않습니다.")
  print(
    f"Jetlink: enabled={b(enabled)} | daemon={b(daemon)} | modeld_socket={b(socket_exists)} | "
    f"display_USB_NexoJetsonUsb={b(display_usb)}"
  )
  if daemon_cmd:
    print(f"daemon process: {daemon_cmd[:220]}")
  if display_error:
    print(f"NexoJetsonUsb 확인 note: {display_error}")

  print(
    f"USB gadget: configfs={b(gadget_exists)} | ffs_mount={b(ffs_exists)} | "
    f"UDC_bound={b(bool(udc))} | UDC={udc or '-'}"
  )
  print(
    f"Jetson link status: state={usb_state} | fresh={b(usb_fresh)} | "
    f"age={f'{usb_age:.1f}s' if usb_age is not None else '-'} | sha={_short_sha(usb_sha) or '-'}"
  )
  if usb_status.get("peer"):
    print(f"Jetson peer: {_compact(usb_status.get('peer'))}")
  if usb_status.get("error"):
    print(f"Jetlink daemon error: {str(usb_status.get('error'))[:300]}")

  print(
    f"modeld Jetlink: ready={b(model_status.get('ready'))} | active={b(model_status.get('active'))} | "
    f"fresh={b(model_fresh)} | age={f'{model_age:.1f}s' if model_age is not None else '-'} | "
    f"sha={_short_sha(model_sha) or '-'}"
  )
  if model_status.get("error"):
    print(f"modeld Jetlink error: {str(model_status.get('error'))[:300]}")

  print(
    f"NEXO model contract: spec={b(bool(spec))} | sha={_short_sha(spec_sha) or '-'} | "
    f"usb_match={b(usb_contract_match)} | modeld_match={b(model_contract_match)} | "
    f"onnx_size_match={b(native_size_match)}"
  )
  if spec:
    print(
      f"contract detail: bytes={spec.get('nbytes','-')} | frame_skip={spec.get('frame_skip','-')} | "
      f"checkpoint={spec.get('checkpoint') or '-'} | inputs={','.join(sorted(spec.get('input_shapes', {}).keys())) or '-'} | "
      f"outputs={len(spec.get('output_slices', {}))}"
    )
  for label, err in (("status", usb_error), ("model_status", model_error), ("spec", spec_error)):
    if err:
      print(f"{label} read note: {err}")

  if FAULT.exists():
    print(f"fault file: present=True | age={f'{fault_age:.1f}s' if fault_age is not None else '-'} | recent={b(recent_fault)}")
  else:
    print("fault file: present=False")

  if not enabled:
    print("[비활성] Jetlink 모델 오프로딩은 OFF입니다. 현재는 기존 콤마 로컬 모델 경로가 기본입니다.")
  elif display_usb is True:
    print("[오류] NexoJetsonUsb 표시용 USB와 Jetlink 추론 USB가 동시에 켜져 있습니다. Jetlink daemon은 conflict 상태여야 합니다.")
  elif not daemon:
    print("[오류] Jetlink가 활성화됐지만 daemon 프로세스가 보이지 않습니다.")
  elif recent_fault:
    print("[오류] 최근 Jetlink active 추론 실패 fault가 기록됐습니다. modeld 재시작/로컬 모델 복귀 여부를 확인하십시오.")
  elif usb_state == "conflict":
    print("[오류] Jetlink USB 충돌 상태입니다. 위 daemon error를 확인하십시오.")
  elif usb_state == "retrying":
    print("[대기] Jetlink daemon이 Jetson USB/server 연결을 재시도 중입니다. 위 error가 현재 원인 후보입니다.")
  elif usb_state in ("connecting", "waiting_model_contract", "stopped", "-") or not usb_fresh:
    print("[대기] Jetlink가 아직 Jetson TensorRT 추론 준비 상태까지 올라오지 않았습니다.")
  elif usb_state == "ready" and usb_contract_match and model_status.get("ready") and model_contract_match and model_fresh:
    if model_status.get("active"):
      print("[정상] Jetlink USB · Jetson TensorRT · NEXO 모델 계약이 일치하며 modeld가 Jetson 추론을 사용 중입니다.")
    else:
      print("[준비] Jetlink USB · Jetson TensorRT · NEXO 모델 계약이 일치합니다. modeld는 아직 로컬 모델 경로이며 안전한 join 조건을 기다리는 상태입니다.")
  elif usb_state == "ready" and usb_contract_match:
    print("[연결] Jetson server/engine은 NEXO 모델 계약으로 준비됐지만 modeld 준비/활성 상태는 아직 확인되지 않았습니다.")
  elif usb_state == "ready":
    print("[오류] Jetson 링크는 ready이지만 NEXO 모델 SHA 계약이 일치하지 않습니다. 외부 추론을 사용하면 안 됩니다.")
  else:
    print("[주의] Jetlink 일부 상태는 확인됐지만 완전한 준비 상태는 아닙니다.")

  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as e:
    print("[31] Jetson Jetlink · TensorRT 모델 오프로딩 진단")
    print(f"Jetlink 진단 내부 오류: {type(e).__name__}: {e}")
    print("※ 추가 진단 오류가 전체 8초 통합진단 완료를 막지 않도록 종료코드는 0으로 반환합니다.")
    raise SystemExit(0)
