import asyncio
import os
import time
import uuid

from aiohttp import web

from ...services.git_status import get_git_status
from . import jobs
from . import nexo_long_logger
from .actions import validate_action
from .dispatcher import dispatch_sync, run_tool_job


NEXO_8SEC_REPORT_PATH = "/data/media/nexo-8sec-diagnostic.txt"


async def api_tools_start(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)

  action = body.get("action")
  action_error = validate_action(action)
  if action_error:
    error, error_code = action_error
    return web.json_response({"ok": False, "error": error, "error_code": error_code}, status=400)

  job_id = uuid.uuid4().hex[:12]
  job = {
    "id": job_id,
    "action": str(action),
    "payload": dict(body),
    "status": "running",
    "log": "",
    "progress": 0,
    "message": "",
    "step_current": 0,
    "step_total": 0,
    "error": None,
    "error_code": None,
    "error_detail": None,
    "result": None,
    "created_at": time.time(),
    "updated_at": time.time(),
  }
  jobs.jobs()[job_id] = job
  jobs.prune()
  jobs.persist_now()
  asyncio.create_task(run_tool_job(job))
  return web.json_response({"ok": True, "job_id": job_id, "status": job["status"]})


async def api_tools_job(request: web.Request) -> web.Response:
  job_id = (request.query.get("id") or request.match_info.get("job_id") or "").strip()
  if not job_id:
    return web.json_response({"ok": False, "error": "missing job id"}, status=400)

  job = jobs.jobs().get(job_id)
  if not job:
    return web.json_response({"ok": False, "error": "job not found"}, status=404)

  return web.json_response(jobs.snapshot(job))


async def api_tools_jobs(request: web.Request) -> web.Response:
  try:
    limit = int((request.query.get("limit") or "").strip() or jobs.TOOL_JOB_KEEP_COUNT)
  except Exception:
    limit = jobs.TOOL_JOB_KEEP_COUNT
  limit = max(1, min(jobs.TOOL_JOB_KEEP_COUNT, limit))
  return web.json_response({"ok": True, "jobs": jobs.list_snapshots(limit)})


async def api_tools_jobs_clear(request: web.Request) -> web.Response:
  removed = jobs.clear_finished()
  return web.json_response({"ok": True, "removed": removed, "jobs": jobs.list_snapshots()})


async def api_tools_jobs_notice(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)
  message = str(body.get("message") or "").strip()
  if not message:
    return web.json_response({"ok": False, "error": "missing message"}, status=400)
  action = str(body.get("action") or "notice").strip() or "notice"
  job = jobs.add_notice(action, message, {"notice": True})
  return web.json_response({"ok": True, "job": job, "jobs": jobs.list_snapshots()})


async def api_tools(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)
  return await dispatch_sync(request, body)


async def api_tools_git_status(request: web.Request) -> web.Response:
  force = any(
    (request.query.get(name) or "").strip().lower() in ("1", "true", "yes")
    for name in ("force", "refresh")
  )
  status = await get_git_status(force=force)
  return web.json_response({"ok": True, **status})


async def nexo_8sec_report(request: web.Request) -> web.StreamResponse:
  # The diagnostic runner writes exactly this fixed file. Expose only that file
  # rather than a generic /download path so the route cannot read arbitrary media.
  if not os.path.isfile(NEXO_8SEC_REPORT_PATH):
    raise web.HTTPNotFound(text="NEXO diagnostic report not ready")
  response = web.FileResponse(NEXO_8SEC_REPORT_PATH)
  response.headers["Cache-Control"] = "no-store"
  return response


async def nexo_long_log_start(request: web.Request) -> web.Response:
  result = nexo_long_logger.start()
  return web.json_response(result, status=200 if result.get("ok") else 409)


async def nexo_long_log_stop(request: web.Request) -> web.Response:
  loop = asyncio.get_running_loop()
  result = await loop.run_in_executor(None, nexo_long_logger.stop)
  status_code = 200 if result.get("ok") else (202 if result.get("processing") else 409)
  return web.json_response(result, status=status_code)


async def nexo_long_log_status(request: web.Request) -> web.Response:
  return web.json_response({"ok": True, **nexo_long_logger.status()})


async def nexo_long_log_report(request: web.Request) -> web.StreamResponse:
  path = nexo_long_logger.report_path()
  if not path or not os.path.isfile(path):
    raise web.HTTPNotFound(text="NEXO long log report not ready")
  response = web.FileResponse(path)
  response.headers["Cache-Control"] = "no-store"
  response.headers["Content-Type"] = "text/plain; charset=utf-8"
  response.headers["Content-Disposition"] = 'inline; filename="nexo-long-log-report.txt"'
  return response


async def nexo_long_log_download(request: web.Request) -> web.StreamResponse:
  path = nexo_long_logger.archive_path()
  if not path or not os.path.isfile(path):
    raise web.HTTPNotFound(text="NEXO long log archive not ready")
  response = web.FileResponse(path)
  response.headers["Cache-Control"] = "no-store"
  response.headers["Content-Disposition"] = 'attachment; filename="nexo-long-log-latest.tar.gz"'
  return response


def register(app: web.Application) -> None:
  jobs.load_persisted()
  app.router.add_post("/api/tools", api_tools)
  app.router.add_post("/api/tools/start", api_tools_start)
  app.router.add_get("/api/tools/job", api_tools_job)
  app.router.add_get("/api/tools/jobs", api_tools_jobs)
  app.router.add_delete("/api/tools/jobs", api_tools_jobs_clear)
  app.router.add_post("/api/tools/jobs/notice", api_tools_jobs_notice)
  app.router.add_get("/api/tools/git_status", api_tools_git_status)
  app.router.add_get("/download/nexo-8sec-diagnostic.txt", nexo_8sec_report)

  # Separate long-running NEXO development logger. The existing 8-second
  # diagnostic route above is intentionally left unchanged.
  app.router.add_post("/api/nexo-long-log/start", nexo_long_log_start)
  app.router.add_post("/api/nexo-long-log/stop", nexo_long_log_stop)
  app.router.add_get("/api/nexo-long-log/status", nexo_long_log_status)
  app.router.add_get("/view/nexo-long-log-report.txt", nexo_long_log_report)
  app.router.add_get("/download/nexo-long-log-latest.tar.gz", nexo_long_log_download)
