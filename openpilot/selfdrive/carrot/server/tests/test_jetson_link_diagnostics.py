"""Regression checks for passive 7000 Jetson/NCM diagnostics."""
from openpilot.selfdrive.carrot.server.services import jetson_link_diagnostics as diag


def _local(**changes):
  local = {"usb_interface": "usb0", "usb_present": True, "usb_admin_up": True,
           "usb_admin_state": "up", "usb_operstate": "up", "usb_carrier": True,
           "usb_carrier_source": "carrier", "enabled": False, "display_usb": False}
  return {**local, **changes}


def _snapshot(connected=False):
  return {"connected": connected, "status": {"camera_frame_seen": None, "model_active": None},
          "native_link": {"state": "unavailable"}, "stages": []}


def _fake_ip(monkeypatch, route_dev="usb0", addr=None, ip_error=""):
  calls = []

  def fake(*args):
    calls.append(args)
    if args[:2] == ("addr", "show"):
      return ([{"ifname": "usb0", "addr_info": [
        {"family": "inet", "local": addr or diag.ncm.COMMA_ADDR}]}], "")
    if args[:2] == ("route", "get"):
      return ([{"dev": route_dev, "src": diag.ncm.COMMA_ADDR}], ip_error)
    raise AssertionError(f"Unexpected probe: {args}")

  monkeypatch.setattr(diag, "_ip_json", fake)
  return calls


def test_connected_ip_and_route_but_no_jetson_heartbeat(monkeypatch, tmp_path):
  monkeypatch.setattr(diag, "GADGET_G1", tmp_path / "no_g1")
  monkeypatch.setattr(diag, "UDC_CLASS", tmp_path / "no_udc")
  calls = _fake_ip(monkeypatch)
  result = diag.collect(_local(), _snapshot())
  states = {item["label"]: item["state"] for item in result["checks"]}
  assert states["USB IP 주소"] == "OK"
  assert states["Jetson 전용 경로"] == "OK"
  assert states["Jetson 앱 상태 신호"] == "WAIT"
  assert states["Jetlink 추론 모드"] == "OFF"
  assert "앱 응답은 미확인" in result["summary"]
  assert len(calls) == 2
  assert all(entry[0] in ("addr", "route") for entry in calls)
  assert "5599 TCP 접속/HELLO" in " ".join(result["notes"])


def test_usb_route_mismatch_explains_problem(monkeypatch):
  _fake_ip(monkeypatch, route_dev="wlan0")
  result = diag.collect(_local(enabled=True), _snapshot())
  assert "라우트" in result["summary"]
  assert next(x for x in result["checks"] if x["label"] == "Jetson 전용 경로")["state"] == "ERROR"


def test_admin_down_not_mistaken_for_no_peer(monkeypatch):
  monkeypatch.setattr(diag, "_ip_json", lambda *args: (None, "route unavailable"))
  result = diag.collect(_local(usb_admin_up=False, usb_admin_state="down", usb_carrier=False), _snapshot())
  assert "DOWN" in result["summary"]
  assert next(x for x in result["checks"] if x["label"] == "USB 인터페이스 활성")["state"] == "ERROR"


def test_disabled_jetlink_is_not_failed_usb(monkeypatch):
  _fake_ip(monkeypatch)
  result = diag.collect(_local(), _snapshot(connected=True))
  text = diag.render_text(result)
  assert "[33] Jetson USB-NCM" in text
  assert "[OFF] Jetlink 추론 모드" in text
  assert "Jetson 앱 상태 신호" in text
  assert "전용 gadget" in text


def test_invalid_interface_never_used_in_system_command(monkeypatch):
  def reject(*args):
    raise AssertionError("invalid interface must not trigger ip")
  monkeypatch.setattr(diag, "_ip_json", reject)
  result = diag.collect(_local(usb_interface="usb0;bad"), _snapshot())
  assert result["interface"] == "usb0;bad"
  assert next(x for x in result["checks"] if x["label"] == "USB IP 주소")["state"] != "OK"
