"""Read-only comma USB/Jetlink preflight, with no UDC or network mutations.

Carrot's FunctionFS Jetlink requires a free *device* UDC. AGNOS may already bind
its ADB/NCM composite gadget (often named g1). A configured USB network does
not mean inference Jetlink is connected. Never evict another gadget here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

CONFIGFS = Path("/sys/kernel/config/usb_gadget")
UDC_ROOT = Path("/sys/class/udc")
USB_NET = Path("/sys/class/net/usb0")


def _read(path: Path) -> str:
  try:
    return path.read_text(encoding="utf-8").strip()
  except (OSError, UnicodeError):
    return ""


def snapshot(configfs: Path = CONFIGFS, udc_root: Path = UDC_ROOT,
             usb_net: Path = USB_NET) -> dict:
  """Inspect sysfs only. No sudo, mounts, kernel writes, or link changes."""
  controllers = sorted(p.name for p in udc_root.iterdir()) if udc_root.is_dir() else []
  gadgets = []
  if configfs.is_dir():
    for base in sorted(configfs.iterdir()):
      if not base.is_dir() or not (base / "UDC").exists():
        continue
      functions = sorted(p.name for p in (base / "functions").iterdir()) if (base / "functions").is_dir() else []
      gadgets.append({"name": base.name, "udc": _read(base / "UDC"), "functions": functions})
  owners = [g for g in gadgets if g["udc"]]
  jetlink_owner = next((g for g in owners if g["name"] == "jetlink"), None)
  blocking = [g for g in owners if g["name"] != "jetlink"]
  if not controllers:
    verdict = "no_udc"
    reason = "콤마의 USB device 컨트롤러를 찾지 못했습니다."
  elif blocking:
    if len(blocking) == 1 and blocking[0]["name"] == "g1":
      verdict = "agnos_g1_busy"
      reason = "AGNOS g1이 USB 컨트롤러를 사용 중입니다. Jetlink FunctionFS gadget은 별도로 바인딩할 수 없습니다."
    else:
      verdict = "other_gadget_busy"
      reason = "Jetlink 이외의 gadget이 USB 컨트롤러를 점유하고 있습니다."
  elif jetlink_owner:
    verdict = "jetlink_bound"
    reason = "Jetlink gadget이 컨트롤러를 사용 중입니다. 서버/모델 준비 상태는 별도로 확인해야 합니다."
  else:
    verdict = "udc_available"
    reason = "현재 바인딩된 다른 gadget은 보이지 않습니다. 이것만으로 Jetlink 연결을 보장하지 않습니다."

  def _state(ctrl: str) -> str:
    return _read(udc_root / ctrl / "state") or "unknown"

  return {
    "verdict": verdict, "reason": reason,
    "controllers": [{"name": ctrl, "state": _state(ctrl)} for ctrl in controllers],
    "owners": owners,
    "usb0": {
      "present": usb_net.exists(),
      "operstate": _read(usb_net / "operstate") or "unknown",
      "carrier": {"1": True, "0": False}.get(_read(usb_net / "carrier")),
    },
    "safety": "g1/UDC를 해제·삭제하지 마십시오. ADB 및 USB 네트워크 접속이 끊길 수 있습니다.",
  }


def format_lines(data: dict) -> list[str]:
  """Display physical enumeration separately from inference readiness."""
  lines = ["[Jetlink USB 사전 점검 · 읽기 전용]"]
  for c in data["controllers"]:
    lines.append(f"UDC {c['name']}: state={c['state']}")
  for g in data["owners"]:
    lines.append(f"USB 점유 {g['name']}: {g['udc']} | functions={','.join(g['functions']) or '-'}")
  net = data["usb0"]
  lines.append(f"usb0: present={net['present']} | carrier={net['carrier']} | operstate={net['operstate']}")
  lines.append(f"판정 {data['verdict']}: {data['reason']}")
  if data["verdict"] == "agnos_g1_busy":
    lines.append("당근 방식도 독립 FunctionFS gadget을 요구합니다. 기존 g1(ADB/NCM)과 자동 공존하지 않습니다.")
    lines.append("USB가 configured여도 Jetlink AI 추론 성공이 아닙니다. 오프로드 상태에서 독립 접속 경로부터 확인하십시오.")
  lines.append(data["safety"])
  return lines


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Read-only NEXO Jetlink USB ownership preflight")
  parser.add_argument("--json", action="store_true", help="Show machine-readable snapshot")
  args = parser.parse_args(argv)
  data = snapshot()
  print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else "\n".join(format_lines(data)))
  return 0  # Observation is never an activation or automatic USB takeover.


if __name__ == "__main__":
  raise SystemExit(main())
