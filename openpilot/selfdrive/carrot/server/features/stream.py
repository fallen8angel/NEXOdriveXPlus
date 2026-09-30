import asyncio
import json
from pathlib import Path
import time

from aiohttp import ClientSession, ClientTimeout, web

from ..config import WEBRTCD_URL
from ..services.vision_diag import record_stream_proxy_event


CLUSTER_WEB_MIRROR_DIR = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path("/tmp")
CLUSTER_WEB_MIRROR_FRAME_PATH = CLUSTER_WEB_MIRROR_DIR / "carrot_cluster_hud_mirror.jpg"
CLUSTER_WEB_MIRROR_REQUEST_PATH = CLUSTER_WEB_MIRROR_DIR / "carrot_cluster_hud_mirror.request"
CLUSTER_WEB_MIRROR_FRAME_MAX_AGE_S = 2.0
CLUSTER_WEB_MIRROR_BOUNDARY = "carrotcluster"


def _request_cluster_hud_mirror() -> None:
  try:
    CLUSTER_WEB_MIRROR_REQUEST_PATH.touch(exist_ok=True)
  except OSError:
    pass


def _cluster_hud_mirror_frame_ready() -> bool:
  try:
    return (time.time() - CLUSTER_WEB_MIRROR_FRAME_PATH.stat().st_mtime) <= CLUSTER_WEB_MIRROR_FRAME_MAX_AGE_S
  except OSError:
    return False


async def cluster_hud_mirror_frame(request: web.Request) -> web.Response:
  if not _cluster_hud_active(request):
    return web.json_response({"ok": False, "error": "cluster HUD is not active"}, status=409)

  _request_cluster_hud_mirror()
  deadline = time.monotonic() + 1.5
  while not _cluster_hud_mirror_frame_ready() and time.monotonic() < deadline:
    await asyncio.sleep(0.05)
  if not _cluster_hud_mirror_frame_ready():
    return web.json_response({"ok": False, "error": "cluster HUD mirror frame unavailable"}, status=503)

  try:
    payload = await asyncio.to_thread(CLUSTER_WEB_MIRROR_FRAME_PATH.read_bytes)
  except OSError as exc:
    return web.json_response({"ok": False, "error": str(exc)}, status=503)
  response = web.Response(body=payload, content_type="image/jpeg")
  response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
  response.headers["Pragma"] = "no-cache"
  return response


async def cluster_hud_mirror_mjpeg(request: web.Request) -> web.StreamResponse:
  if not _cluster_hud_active(request):
    return web.json_response({"ok": False, "error": "cluster HUD is not active"}, status=409)

  response = web.StreamResponse(
    status=200,
    headers={
      "Content-Type": f"multipart/x-mixed-replace; boundary={CLUSTER_WEB_MIRROR_BOUNDARY}",
      "Cache-Control": "no-store, no-cache, must-revalidate",
      "Pragma": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  )
  await response.prepare(request)

  last_mtime_ns = -1
  last_request_touch = 0.0
  try:
    while True:
      now = time.monotonic()
      if now - last_request_touch >= 0.5:
        _request_cluster_hud_mirror()
        last_request_touch = now

      try:
        stat = CLUSTER_WEB_MIRROR_FRAME_PATH.stat()
      except OSError:
        await asyncio.sleep(0.05)
        continue

      if (time.time() - stat.st_mtime) > CLUSTER_WEB_MIRROR_FRAME_MAX_AGE_S:
        await asyncio.sleep(0.05)
        continue
      if stat.st_mtime_ns == last_mtime_ns:
        await asyncio.sleep(0.03)
        continue

      try:
        payload = await asyncio.to_thread(CLUSTER_WEB_MIRROR_FRAME_PATH.read_bytes)
      except OSError:
        await asyncio.sleep(0.03)
        continue
      if not payload:
        await asyncio.sleep(0.03)
        continue

      last_mtime_ns = stat.st_mtime_ns
      header = (
        f"--{CLUSTER_WEB_MIRROR_BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
      ).encode("ascii")
      await response.write(header)
      await response.write(payload)
      await response.write(b"\r\n")
  except asyncio.CancelledError:
    raise
  except (ConnectionResetError, BrokenPipeError, RuntimeError):
    pass

  return response


def _cluster_hud_active(request: web.Request) -> bool:
  params = request.app.get("params")
  try:
    return params is not None and params.get_int("ClusterHud") == 1
  except Exception:
    return False


def _request_summary(body: bytes) -> dict:
  try:
    payload = json.loads(body.decode("utf-8", errors="replace"))
    return {
      "cameras": payload.get("cameras"),
      "bridge_services_in": payload.get("bridge_services_in"),
      "bridge_services_out": payload.get("bridge_services_out"),
      "client_id": str(payload.get("client_id") or "")[:128],
      "takeover": bool(payload.get("takeover")),
      "carrot_state": bool(payload.get("carrot_state")),
      "sdp_bytes": len(str(payload.get("sdp") or "")),
    }
  except Exception:
    return {}


async def proxy_stream(request: web.Request) -> web.StreamResponse:
  body = await request.read()
  ct = request.headers.get("Content-Type", "application/json")
  started_at = time.monotonic()
  base_event = {
    "client": request.remote or "",
    "content_type": ct,
    "request_bytes": len(body),
    **_request_summary(body),
  }

  if _cluster_hud_active(request):
    record_stream_proxy_event({
      **base_event,
      "ok": False,
      "status": 409,
      "error": "cluster HUD active",
      "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
    })
    return web.json_response({
      "ok": False,
      "error": "Carrot Vision is unavailable while Cluster HUD is active",
      "code": "cluster_hud_active",
    }, status=409)

  sess: ClientSession = request.app["http"]

  try:
    async with sess.post(WEBRTCD_URL, data=body, headers={"Content-Type": ct},
                         timeout=ClientTimeout(total=5)) as resp:
      resp_body = await resp.read()
      out = web.Response(body=resp_body, status=resp.status)
      rct = resp.headers.get("Content-Type")
      if rct:
        out.headers["Content-Type"] = rct
      record_stream_proxy_event({
        **base_event,
        "ok": 200 <= resp.status < 300,
        "status": resp.status,
        "response_bytes": len(resp_body),
        "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
      })
      return out
  except asyncio.CancelledError:
    raise
  except TimeoutError:
    record_stream_proxy_event({
      **base_event,
      "ok": False,
      "status": 504,
      "error": "webrtcd timeout",
      "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
    })
    return web.json_response({"ok": False, "error": "webrtcd timeout"}, status=504)
  except Exception as e:
    record_stream_proxy_event({
      **base_event,
      "ok": False,
      "status": 502,
      "error": str(e),
      "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
    })
    return web.json_response({"ok": False, "error": str(e)}, status=502)


def register(app: web.Application) -> None:
  app.router.add_post("/stream", proxy_stream)
  app.router.add_get("/api/cluster_hud/frame.jpg", cluster_hud_mirror_frame)
  app.router.add_get("/api/cluster_hud/mjpeg", cluster_hud_mirror_mjpeg)
