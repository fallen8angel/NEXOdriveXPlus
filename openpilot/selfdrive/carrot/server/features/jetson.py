from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from aiohttp import web


JETSON_STATUS_PORT = 8766
JETSON_STATUS_MAGIC = "NEXO_JETSON_STATUS"
JETSON_FRESH_SECONDS = 5.0

_STATUS_FIELDS = (
  "host",
  "ip",
  "comma_ip",
  "state",
  "diagnosis",
  "service_active",
  "pipeline",
  "pipeline_mode",
  "yolo_proc",
  "comma_tcp",
  "packet_seen",
  "iframe_seen",
  "camera_frame_seen",
  "yolo_recent",
  "ready",
  "last_det",
  "last_infer_ms",
  "last_error",
)


class _JetsonStatusProtocol(asyncio.DatagramProtocol):
  def __init__(self, state: dict[str, Any]) -> None:
    self.state = state

  def datagram_received(self, data: bytes, _addr) -> None:
    if len(data) > 64 * 1024:
      return
    try:
      payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
      return
    if not isinstance(payload, dict) or payload.get("magic") != JETSON_STATUS_MAGIC:
      return

    self.state["packet"] = {key: payload.get(key) for key in _STATUS_FIELDS if key in payload}
    self.state["received_mono"] = time.monotonic()
    self.state["error"] = ""

  def error_received(self, exc: Exception) -> None:
    self.state["error"] = str(exc)


async def _start_receiver(app: web.Application) -> None:
  state = app["jetson_status_state"]
  try:
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
      lambda: _JetsonStatusProtocol(state),
      local_addr=("0.0.0.0", JETSON_STATUS_PORT),
    )
    state["transport"] = transport
    state["error"] = ""
  except Exception as exc:
    # Jetson monitoring is optional. Never prevent Carrot Web from starting.
    state["transport"] = None
    state["error"] = f"status receiver unavailable: {exc}"


async def _stop_receiver(app: web.Application) -> None:
  state = app.get("jetson_status_state")
  if not state:
    return
  transport = state.get("transport")
  if transport is not None:
    transport.close()
    state["transport"] = None


async def api_jetson_status(request: web.Request) -> web.Response:
  state = request.app["jetson_status_state"]
  packet = state.get("packet")
  received = float(state.get("received_mono") or 0.0)
  age_s = max(0.0, time.monotonic() - received) if received > 0 else None
  connected = bool(packet is not None and age_s is not None and age_s <= JETSON_FRESH_SECONDS)

  return web.json_response({
    "ok": True,
    "connected": connected,
    "age_ms": int(age_s * 1000) if age_s is not None else None,
    "receiver_active": state.get("transport") is not None,
    "receiver_error": str(state.get("error") or ""),
    "status": packet or {},
  })


async def jetson_page(_request: web.Request) -> web.Response:
  html = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <title>Jetson 상태</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, -apple-system, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #0b1016; color: #eef2f6; min-height: 100vh; }
    main { width: min(720px, 100%); margin: 0 auto; padding: 20px 16px 40px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 24px; }
    a { color: #d7e3f0; text-decoration: none; border: 1px solid #46515d; border-radius: 999px; padding: 9px 14px; }
    .hero { border: 1px solid #303b47; border-radius: 18px; padding: 18px; background: #141b23; margin-bottom: 12px; }
    .state { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 20px; }
    .dot { width: 12px; height: 12px; border-radius: 50%; background: #6f7a85; }
    .online .dot { background: #55d58b; box-shadow: 0 0 14px rgba(85,213,139,.45); }
    .offline .dot { background: #ff7c73; }
    .sub { color: #9eabb8; margin-top: 7px; font-size: 14px; overflow-wrap: anywhere; }
    .grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; }
    .card { min-width: 0; border: 1px solid #303b47; border-radius: 14px; padding: 14px; background: #111820; }
    .label { color: #8f9ba8; font-size: 12px; margin-bottom: 5px; }
    .value { font-size: 16px; font-weight: 750; overflow-wrap: anywhere; }
    .wide { grid-column: 1 / -1; }
    @media (max-width: 520px) { .grid { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
  </style>
</head>
<body>
<main>
  <header><h1>NVIDIA Jetson</h1><a href="/">← 7000 홈</a></header>
  <section id="hero" class="hero offline">
    <div class="state"><span class="dot"></span><span id="stateText">상태 확인 중…</span></div>
    <div id="diagnosis" class="sub">젯슨 상태 신호를 기다리고 있습니다.</div>
  </section>
  <section class="grid">
    <div class="card"><div class="label">Jetson IP</div><div id="jetsonIp" class="value">-</div></div>
    <div class="card"><div class="label">YOLO</div><div id="yolo" class="value">-</div></div>
    <div class="card"><div class="label">Pipeline</div><div id="pipeline" class="value">-</div></div>
    <div class="card"><div class="label">Comma 연결</div><div id="commaTcp" class="value">-</div></div>
    <div class="card"><div class="label">카메라 프레임</div><div id="frame" class="value">-</div></div>
    <div class="card"><div class="label">YOLO 최근 추론</div><div id="recent" class="value">-</div></div>
    <div class="card"><div class="label">최근 객체 수</div><div id="det" class="value">-</div></div>
    <div class="card"><div class="label">추론 시간</div><div id="infer" class="value">-</div></div>
    <div class="card wide"><div class="label">수신기</div><div id="receiver" class="value">-</div></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const yn = (v) => v === true ? "정상" : v === false ? "대기" : "-";
async function refresh() {
  try {
    const r = await fetch('/api/jetson/status', {cache: 'no-store'});
    const data = await r.json();
    const s = data.status || {};
    const connected = !!data.connected;
    $('hero').className = 'hero ' + (connected ? 'online' : 'offline');
    $('stateText').textContent = connected ? 'Jetson 연결됨' : 'Jetson 신호 없음';
    $('diagnosis').textContent = connected ? (s.diagnosis || s.state || '상태 신호 수신 중') : '최근 5초 이내 상태 신호가 없습니다.';
    $('jetsonIp').textContent = s.ip || '-';
    $('yolo').textContent = yn(s.yolo_proc);
    $('pipeline').textContent = s.pipeline || s.pipeline_mode || '-';
    $('commaTcp').textContent = yn(s.comma_tcp);
    $('frame').textContent = yn(s.camera_frame_seen);
    $('recent').textContent = yn(s.yolo_recent);
    $('det').textContent = s.last_det ?? '-';
    $('infer').textContent = s.last_infer_ms == null ? '-' : `${s.last_infer_ms} ms`;
    $('receiver').textContent = data.receiver_active ? `정상 · 신호 ${data.age_ms == null ? '-' : data.age_ms + ' ms 전'}` : (data.receiver_error || '비활성');
  } catch (e) {
    $('hero').className = 'hero offline';
    $('stateText').textContent = '상태 확인 실패';
    $('diagnosis').textContent = String(e);
  }
}
refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>"""
  return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


def register(app: web.Application) -> None:
  app["jetson_status_state"] = {
    "packet": None,
    "received_mono": 0.0,
    "transport": None,
    "error": "",
  }
  app.on_startup.append(_start_receiver)
  app.on_cleanup.append(_stop_receiver)
  app.router.add_get("/api/jetson/status", api_jetson_status)
  app.router.add_get("/jetson", jetson_page)
