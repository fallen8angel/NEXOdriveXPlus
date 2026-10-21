"""Read-only Jetson USB-NCM evidence for 7000 and the existing TXT diagnostic.

This module NEVER connects to port 5599 or issues HELLO: that server may have
a single active Jetlink session. Never repair the USB route or start inference
from a status request, especially while the vehicle is onroad.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from openpilot.selfdrive.modeld.jetlink import ncm_transport as ncm


GADGET_G1 = Path("/sys/kernel/config/usb_gadget/g1")
UDC_CLASS = Path("/sys/class/udc")
_INTERFACE = re.compile(r"[a-zA-Z0-9_.:-]{1,32}\Z")


def _read(path: Path) -> str | None:
  try:
    return path.read_text(encoding="utf-8", errors="replace").strip()[:160]
  except OSError:
    return None


def _ip_json(*args: str) -> tuple[list[dict] | None, str]:
  """Observe only; do not send packets or alter interfaces/routes."""
  try:
    process = subprocess.run(
      ["ip", "-j", "-4", *args], capture_output=True, text=True, timeout=0.8, check=False,
    )
    if process.returncode:
      return None, (process.stderr or "ip returned nonzero")[:140].strip()
    result = json.loads(process.stdout)
    return (result, "") if isinstance(result, list) else (None, "unexpected ip JSON")
  except (OSError, ValueError, subprocess.SubprocessError) as exc:
    return None, f"{type(exc).__name__}: {exc}"[:140]


def _record(label: str, state: str, detail: str, advice: str = "") -> dict:
  return {"label": label, "state": state, "detail": detail[:240], "advice": advice[:240]}


def collect(local: dict, result: dict) -> dict:
  """Observe the comma side only; lack of evidence is not a peer failure."""
  local = local if isinstance(local, dict) else {}
  result = result if isinstance(result, dict) else {}
  checks = []
  notes = []
  iface = str(local.get("usb_interface") or "")
  valid_iface = bool(_INTERFACE.fullmatch(iface))
  g1 = GADGET_G1.is_dir()
  udc = _read(GADGET_G1 / "UDC") if g1 else None
  try:
    udc_devices = sorted(p.name for p in UDC_CLASS.iterdir())[:8]
  except OSError:
    udc_devices = []
  try:
    functions = sorted(p.name for p in (GADGET_G1 / "functions").iterdir())[:20] if g1 else []
  except OSError:
    functions = []
  ncm_function = any(name.startswith(("ncm.", "ecm.")) for name in functions)
  checks.append(_record(
    "USB 컨트롤러 / g1", "OK" if g1 and udc else "UNKNOWN",
    f"UDC 후보={','.join(udc_devices) or '없음/읽기 불가'} | g1={g1} | g1 UDC={udc or '-'} | NCM function={ncm_function}",
    "g1의 UDC가 비어 있다면 USB 구성 상태를 정차 후 확인하십시오. Jetlink 전용 gadget 없음은 NCM 고장이 아닙니다.",
  ))
  exists = local.get("usb_present")
  admin = local.get("usb_admin_up")
  carrier = local.get("usb_carrier")
  discovery_error = str(local.get("usb_discovery_error") or "")
  checks.append(_record(
    "USB 네트워크 장치", "OK" if exists is True else "ERROR" if exists is False else "UNKNOWN",
    f"인터페이스={iface or '미발견'} | 존재={exists} | 탐색={discovery_error or '-'}",
    "NCM USB 함수 및 케이블·전원·부팅 순서를 확인하십시오.",
  ))
  checks.append(_record(
    "USB 인터페이스 활성", "OK" if admin is True else "ERROR" if admin is False else "UNKNOWN",
    f"admin={local.get('usb_admin_state', 'unknown')} | operstate={local.get('usb_operstate', 'unknown')}",
    "DOWN이면 정차 상태에서 자동 연결 서비스와 인터페이스 초기화 기록을 확인하십시오. 이 진단은 활성화하지 않습니다.",
  ))
  checks.append(_record(
    "USB 물리 링크", "OK" if carrier is True and admin is True else
    "WAIT" if admin is False or carrier is False else "UNKNOWN",
    f"carrier={carrier} | 출처={local.get('usb_carrier_source', 'unknown')} | 오류={local.get('usb_carrier_error') or '-'}",
    "USB 데이터 케이블, 전원, Jetson 부팅·NCM 모드를 확인하십시오. carrier만으로 통신 성공을 보장하지 않습니다.",
  ))

  local_addrs = []
  addr_error = ""
  route = {}
  route_error = ""
  if valid_iface and exists is True:
    address_data, addr_error = _ip_json("addr", "show", "dev", iface)
    if address_data is not None:
      for item in address_data:
        if item.get("ifname") == iface:
          local_addrs.extend(a.get("local") for a in item.get("addr_info", [])
                             if isinstance(a, dict) and a.get("family") == "inet" and isinstance(a.get("local"), str))
    route_data, route_error = _ip_json("route", "get", ncm.JETSON_ADDR)
    if route_data:
      route = route_data[0] if isinstance(route_data[0], dict) else {}
  addr_verified = ncm.COMMA_ADDR in local_addrs
  addr_known = bool(local_addrs)
  addr_state = "OK" if addr_verified else "ERROR" if addr_known else "WAIT" if not addr_error else "UNKNOWN"
  checks.append(_record(
    "USB IP 주소", addr_state,
    f"인터페이스={iface or '-'} | 주소={','.join(local_addrs) or '-'} | 기대={ncm.COMMA_ADDR} | 조회={addr_error or '완료'}",
    "USB 주소가 누락되거나 다르면 자동 연결 서비스의 IP 할당 단계를 확인하십시오.",
  ))
  route_verified = bool(route and route.get("dev") == iface and (route.get("src") or route.get("prefsrc") or ncm.COMMA_ADDR) == ncm.COMMA_ADDR)
  route_state = "OK" if route_verified else "ERROR" if route else "WAIT" if not route_error else "UNKNOWN"
  checks.append(_record(
    "Jetson 전용 경로", route_state,
    f"목적지={ncm.JETSON_ADDR} | route dev={route.get('dev') or '-'} | src={route.get('prefsrc') or route.get('src') or '-'} | 조회={route_error or '완료'}",
    "Jetson IP로 가는 경로가 NCM USB를 가리키는지 정차 후 확인하십시오. Wi-Fi 자동 우회는 금지합니다.",
  ))

  connected = result.get("connected") is True
  native = result.get("native_link") if isinstance(result.get("native_link"), dict) else {}
  status = result.get("status") if isinstance(result.get("status"), dict) else {}
  stages = {v.get("id"): v for v in result.get("stages", []) if isinstance(v, dict)}
  enabled = local.get("enabled") is True
  display_enabled = local.get("display_usb") is True
  checks.append(_record(
    "Jetson 앱 상태 신호", "OK" if connected else "WAIT",
    "최근 검증된 연결 상태 기록 있음" if connected else "최근 Jetson heartbeat·NCM 세션 기록 없음",
    "원격 Jetson 상태는 수신 기록이 없으면 확인 불가입니다. 물리 연결과 별도로 확인하십시오.",
  ))
  checks.append(_record(
    "Jetlink 추론 모드", "ERROR" if local.get("conflict") else "WAIT" if enabled else "OFF",
    f"Jetlink enabled={enabled} | 표시용 USB={display_enabled} | daemon_state={native.get('state') or 'unavailable'}",
    "OFF는 의도된 설정일 수 있습니다. 진단이 추론을 강제로 시작하지 않습니다.",
  ))
  checks.append(_record(
    "카메라 TX / 추론", "OK" if status.get("model_active") is True and status.get("camera_frame_seen") is True
    else "WAIT",
    f"Camera={stages.get('camera', {}).get('state', 'UNKNOWN')} | TX={stages.get('camera_tx', {}).get('state', 'UNKNOWN')} | "
    f"Inference={stages.get('inference', {}).get('state', 'UNKNOWN')}",
    "카메라 송신, Jetson 수신, 추론 응답은 서로 다른 증거입니다. 미관측을 성공으로 표시하지 않습니다.",
  ))
  if local.get("conflict"):
    summary = "표시 USB와 Jetlink 추론이 동시에 활성화된 소유권 충돌 후보입니다."
  elif exists is False:
    summary = "콤마에서 NCM USB 네트워크 장치를 찾지 못했습니다. USB 함수·케이블·부팅 상태를 확인하십시오."
  elif admin is False:
    summary = "콤마 USB 인터페이스가 DOWN입니다. 연결 자동 복구 서비스와 USB 인식을 먼저 확인하십시오."
  elif carrier is False:
    summary = "USB 네트워크 carrier가 확인되지 않았습니다. 케이블·젯슨 전원·NCM 구성이 원인 후보입니다."
  elif addr_known and not addr_verified:
    summary = "USB IP가 기대값과 다릅니다. NCM 주소 구성을 확인하십시오."
  elif addr_verified and route and not route_verified:
    summary = "Jetson 목적지 라우트가 올바른 USB 인터페이스를 사용하지 않습니다."
  elif connected:
    summary = "Jetson 앱 상태 신호는 확인됐습니다. 실제 TensorRT 추론과 외부 HUD 화면은 별도로 검증해야 합니다."
  elif carrier is True and addr_verified and route_verified:
    summary = "콤마 USB·IP·라우트는 확인됐지만 Jetson 앱 응답은 미확인입니다. 원격 서비스나 기존 세션 상태를 확인하십시오."
  elif not enabled:
    summary = "Jetlink 추론은 OFF입니다. USB 물리 연결과 Jetson 앱 응답을 분리하여 확인하십시오."
  else:
    summary = "Jetson 연결 증거가 부족합니다. 아래 단계를 순서대로 확인하십시오."
  notes.append("읽기 전용: CAN·Panda·USB·Params·시스템 서비스를 변경하지 않습니다.")
  notes.append("5599 TCP 접속/HELLO·ping·SSH는 실행하지 않습니다. 원격 Jetson 전원과 실제 데이터 수신은 미확인일 수 있습니다.")
  notes.append("g1 NCM과 Jetlink 전용 gadget은 서로 다릅니다. OFF 상태의 전용 configfs/UDC 미생성만으로 물리 USB 장애로 판정하지 않습니다.")
  return {"summary": summary, "checks": checks, "notes": notes, "interface": iface or None,
          "local_ip": local_addrs, "jetson_ip": ncm.JETSON_ADDR, "route_device": route.get("dev") or None}


def render_text(diagnostic: dict) -> str:
  lines = ["[31-A] Jetson USB-NCM · 차내 터미널 없는 상세 진단", diagnostic.get("summary", "확인 불가")]
  for entry in diagnostic.get("checks", []):
    lines.append(f"[{entry['state']}] {entry['label']}: {entry['detail']}")
    if entry.get("advice") and entry.get("state") not in ("OK", "OFF"):
      lines.append("  확인: " + entry["advice"])
  lines.extend("※ " + note for note in diagnostic.get("notes", []))
  return "\n".join(lines)
