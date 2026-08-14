from __future__ import annotations

import csv
import json
import os
import struct
import subprocess
import tarfile
import threading
import time
import traceback
from collections import Counter
from datetime import datetime
from typing import Any

from openpilot.cereal import log, messaging
from openpilot.cereal.services import SERVICE_LIST
from openpilot.common.params import Params


REPO_ROOT = "/data/openpilot"
BASE_DIR = "/data/media/nexo-long-logs"
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LATEST_ARCHIVE = "/data/media/nexo-long-log-latest.tar.gz"

DESIRED_SERVICES = [
  "can",
  "sendcan",
  "carState",
  "carControl",
  "carOutput",
  "controlsState",
  "selfdriveState",
  "longitudinalPlan",
  "radarState",
  "liveTracks",
  "pandaStates",
  "carParams",
  "onroadEvents",
  "deviceState",
  "managerState",
  "liveParameters",
  "liveTorqueParameters",
  "liveCalibration",
  "liveDelay",
  "liveLocationKalman",
]

HYUNDAI_SCC_IDS = {
  0x420: "SCC11",
  0x421: "SCC12",
  0x50A: "SCC13",
  0x389: "SCC14",
  0x38D: "FCA11",
}
RADAR_TRACK_IDS = set(range(0x500, 0x520))
UDS_IDS = {0x7D0: "UDS_REQ_7D0", 0x7D8: "UDS_RESP_7D8"}
BUTTON_IDS = {
  0x4F1: "CLU11",
  0x3EF: "CRUISE_BUTTON_ALT",
  0x391: "BCM_PO_11/LFA",
  0x416: "CRUISE_BUTTON_LFA",
}
SOURCE_SCAN_TOKENS = (
  "0x7D0",
  "0x7D8",
  "SCC11",
  "SCC12",
  "SCC13",
  "SCC14",
  "FCA11",
  "openpilotLongitudinalControl",
  "safetyParam",
  "disable_ecu",
  "radar",
  "Med",
  "MED",
)
SOURCE_SCAN_ROOTS = (
  "openpilot/selfdrive/car/hyundai",
  "openpilot/selfdrive/carrot",
  "panda/board/safety",
)
SAFE_PARAM_TOKENS = (
  "car", "cruise", "long", "scc", "radar", "nexo", "hyundai", "carrot",
  "mad", "med", "safety", "steer", "torque", "button", "enable",
  "experimental", "speed", "controls", "firmware",
)
SECRET_PARAM_TOKENS = (
  "token", "secret", "password", "passwd", "ssh", "auth", "cookie",
  "dongleid", "access", "private", "credential",
)

_MAGIC = b"NXLPLOG1\n"
_RECORD = struct.Struct(">QHI")  # receive monotonic ns, service index, payload length

_lock = threading.RLock()
_stop_event: threading.Event | None = None
_worker: threading.Thread | None = None
_state: dict[str, Any] = {
  "active": False,
  "finalizing": False,
  "finished": False,
  "session": None,
  "started_at": None,
  "started_mono": None,
  "stopped_at": None,
  "elapsed": 0.0,
  "report_path": None,
  "archive_path": None,
  "error": None,
}


def _atomic_json(path: str, obj: Any) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = f"{path}.{os.getpid()}.tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    f.flush()
    os.fsync(f.fileno())
  os.replace(tmp, path)


def _persist_state() -> None:
  with _lock:
    state = dict(_state)
  try:
    _atomic_json(STATE_PATH, state)
  except Exception:
    pass


def _load_state() -> None:
  global _state
  try:
    with open(STATE_PATH, "r", encoding="utf-8") as f:
      saved = json.load(f)
    if not isinstance(saved, dict):
      return
    # A browser refresh is fine, but a server restart cannot keep the worker
    # thread alive. Mark a stale active session as interrupted instead of
    # falsely reporting that recording is still running.
    if saved.get("active"):
      saved["active"] = False
      saved["finalizing"] = False
      saved["finished"] = False
      saved["error"] = "carrot server가 재시작되어 이전 장시간 기록이 중단되었습니다."
    _state.update(saved)
  except Exception:
    pass


def _run(args: list[str], timeout: float = 5.0) -> str:
  try:
    p = subprocess.run(
      args,
      cwd=REPO_ROOT,
      capture_output=True,
      text=True,
      timeout=timeout,
    )
    text = ((p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")).strip()
    if p.returncode != 0:
      return f"[exit={p.returncode}] {text}".strip()
    return text
  except Exception as e:
    return f"[{type(e).__name__}] {e}"


def _git_snapshot(session_dir: str) -> dict[str, str]:
  branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
  commit = _run(["git", "rev-parse", "HEAD"])
  status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], timeout=10.0)
  diff = _run(["git", "diff", "--no-ext-diff", "--binary"], timeout=20.0)
  staged = _run(["git", "diff", "--cached", "--no-ext-diff", "--binary"], timeout=20.0)

  with open(os.path.join(session_dir, "git_status.txt"), "w", encoding="utf-8") as f:
    f.write(f"branch: {branch}\ncommit: {commit}\n")
    f.write("dirty: " + ("False" if not status else "True") + "\n\n")
    f.write(status or "(clean)")
    f.write("\n")
  with open(os.path.join(session_dir, "git_diff.patch"), "w", encoding="utf-8") as f:
    f.write(diff)
    if staged:
      f.write("\n\n# ---- staged diff ----\n")
      f.write(staged)

  return {"branch": branch, "commit": commit, "dirty": str(bool(status))}


def _param_snapshot(session_dir: str, label: str) -> None:
  params_dir = "/data/params/d"
  result: dict[str, Any] = {}
  try:
    names = sorted(os.listdir(params_dir))
  except Exception:
    names = []

  for name in names:
    lower = name.lower()
    if not any(token in lower for token in SAFE_PARAM_TOKENS):
      continue
    path = os.path.join(params_dir, name)
    try:
      if not os.path.isfile(path):
        continue
      raw = open(path, "rb").read(65537)
      if len(raw) > 65536:
        result[name] = "<value larger than 64 KiB omitted>"
        continue
      if any(token in lower for token in SECRET_PARAM_TOKENS):
        result[name] = "<redacted>"
        continue
      try:
        result[name] = raw.decode("utf-8")
      except UnicodeDecodeError:
        result[name] = {"binary_hex": raw.hex().upper()}
    except Exception as e:
      result[name] = f"<read error: {type(e).__name__}: {e}>"

  _atomic_json(os.path.join(session_dir, f"params_{label}.json"), result)

  try:
    cp = Params().get("CarParams")
    if cp:
      with open(os.path.join(session_dir, f"CarParams_{label}.bin"), "wb") as f:
        f.write(cp)
  except Exception:
    pass


def _source_scan(session_dir: str) -> None:
  out_path = os.path.join(session_dir, "source_hits.txt")
  max_file_size = 2 * 1024 * 1024
  max_hits = 12000
  hits = 0
  with open(out_path, "w", encoding="utf-8") as out:
    out.write("NEXO longitudinal source evidence\n")
    out.write("tokens: " + ", ".join(SOURCE_SCAN_TOKENS) + "\n\n")
    for rel_root in SOURCE_SCAN_ROOTS:
      root = os.path.join(REPO_ROOT, rel_root)
      if not os.path.isdir(root):
        continue
      for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "build")]
        for filename in filenames:
          path = os.path.join(dirpath, filename)
          try:
            if os.path.getsize(path) > max_file_size:
              continue
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
              for lineno, line in enumerate(f, 1):
                if any(token in line for token in SOURCE_SCAN_TOKENS):
                  rel = os.path.relpath(path, REPO_ROOT)
                  out.write(f"{rel}:{lineno}: {line.rstrip()}\n")
                  hits += 1
                  if hits >= max_hits:
                    out.write("\n[truncated: hit limit reached]\n")
                    return
          except Exception:
            continue


def _route_log_references(start_epoch: float, stop_epoch: float, session_dir: str) -> list[dict[str, Any]]:
  roots = ("/data/media/0/realdata", "/data/media/0")
  seen: set[str] = set()
  refs: list[dict[str, Any]] = []
  lo = start_epoch - 15.0
  hi = stop_epoch + 15.0

  for root in roots:
    if not os.path.isdir(root):
      continue
    for dirpath, dirnames, filenames in os.walk(root):
      if root == "/data/media/0":
        # Avoid scanning camera/video stores and the long-log output itself.
        dirnames[:] = [d for d in dirnames if d not in (
          "dcam", "fcam", "ecam", "video", "videos", "nexo-long-logs"
        )]
      for filename in filenames:
        lower = filename.lower()
        if "rlog" not in lower and "qlog" not in lower:
          continue
        path = os.path.join(dirpath, filename)
        if path in seen:
          continue
        seen.add(path)
        try:
          st = os.stat(path)
        except Exception:
          continue
        # Include files that were created/modified around the capture window.
        if st.st_mtime < lo or st.st_mtime > hi:
          continue
        refs.append({
          "path": path,
          "size": st.st_size,
          "mtime": st.st_mtime,
          "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })

  refs.sort(key=lambda x: (x["mtime"], x["path"]))
  _atomic_json(os.path.join(session_dir, "matching_rlog_qlog.json"), refs)
  return refs


def _json_default(value: Any) -> Any:
  if isinstance(value, (bytes, bytearray, memoryview)):
    return {"__bytes_hex": bytes(value).hex().upper()}
  try:
    return value.to_dict()
  except Exception:
    pass
  try:
    return list(value)
  except Exception:
    return str(value)


def _payload_to_json(value: Any) -> Any:
  if isinstance(value, (str, int, float, bool)) or value is None:
    return value
  if isinstance(value, (bytes, bytearray, memoryview)):
    return {"__bytes_hex": bytes(value).hex().upper()}
  if isinstance(value, dict):
    return {str(k): _payload_to_json(v) for k, v in value.items()}
  try:
    if hasattr(value, "to_dict"):
      return _payload_to_json(value.to_dict())
  except Exception:
    pass
  try:
    return [_payload_to_json(item) for item in value]
  except Exception:
    return str(value)


def _safe(obj: Any, name: str, default: Any = None) -> Any:
  try:
    return getattr(obj, name)
  except Exception:
    return default


def _enum_name(value: Any) -> str:
  try:
    return str(value).split(".")[-1]
  except Exception:
    return str(value)


def _write_report(
  session_dir: str,
  session: str,
  start_epoch: float,
  stop_epoch: float,
  git_info: dict[str, str],
  services: list[str],
  missing_services: list[str],
  service_counts: Counter,
  frame_counts: Counter,
  latest_json: dict[str, Any],
  worker_errors: list[str],
  route_refs: list[dict[str, Any]],
) -> str:
  report_path = os.path.join(session_dir, "report.txt")
  elapsed = max(0.0, stop_epoch - start_epoch)

  def direction_rows(direction: str, addresses: set[int] | dict[int, str]) -> list[str]:
    wanted = set(addresses)
    rows = []
    for (d, bus, address), count in sorted(frame_counts.items()):
      if d == direction and address in wanted:
        label = ""
        if isinstance(addresses, dict):
          label = f" {addresses.get(address, '')}".rstrip()
        rows.append(f"{direction} bus={bus} 0x{address:X}{label}: {count}")
    return rows

  radar_rows = []
  for (direction, bus, address), count in sorted(frame_counts.items()):
    if address in RADAR_TRACK_IDS:
      radar_rows.append(f"{direction} bus={bus} 0x{address:X}: {count}")

  with open(report_path, "w", encoding="utf-8") as f:
    f.write("=" * 76 + "\n")
    f.write("NEXOdriveXPlus 장시간 NexoPilot 개발 로그\n")
    f.write("=" * 76 + "\n")
    f.write(f"session: {session}\n")
    f.write(f"start: {datetime.fromtimestamp(start_epoch).isoformat(timespec='seconds')}\n")
    f.write(f"stop : {datetime.fromtimestamp(stop_epoch).isoformat(timespec='seconds')}\n")
    f.write(f"elapsed: {elapsed:.1f} sec\n\n")

    f.write("[1] Git / code identity\n")
    f.write(f"branch: {git_info.get('branch', '-')}\n")
    f.write(f"commit: {git_info.get('commit', '-')}\n")
    f.write(f"dirty: {git_info.get('dirty', '-')}\n")
    f.write("git_status.txt / git_diff.patch / source_hits.txt included\n\n")

    f.write("[2] Captured cereal services\n")
    for service in services:
      f.write(f"{service}: {service_counts[service]} messages\n")
    if missing_services:
      f.write("not available in this build: " + ", ".join(missing_services) + "\n")
    f.write("\n")

    f.write("[3] SCC/FCA raw CAN evidence\n")
    scc_rows = direction_rows("RX", HYUNDAI_SCC_IDS) + direction_rows("TX", HYUNDAI_SCC_IDS)
    f.write("\n".join(scc_rows) + ("\n" if scc_rows else "no SCC/FCA frames observed\n"))
    f.write("\n")

    f.write("[4] Radar track 0x500~0x51F\n")
    f.write("\n".join(radar_rows) + ("\n" if radar_rows else "no 0x500~0x51F frames observed\n"))
    f.write("\n")

    f.write("[5] UDS 0x7D0 / 0x7D8\n")
    uds_rows = direction_rows("RX", UDS_IDS) + direction_rows("TX", UDS_IDS)
    f.write("\n".join(uds_rows) + ("\n" if uds_rows else "no 0x7D0/0x7D8 traffic observed during this recording\n"))
    f.write("※ startup-only UDS may occur before manual recording; source_hits.txt + Git commit preserve the code-side evidence.\n\n")

    f.write("[6] Cruise / button raw CAN\n")
    button_rows = direction_rows("RX", BUTTON_IDS) + direction_rows("TX", BUTTON_IDS)
    f.write("\n".join(button_rows) + ("\n" if button_rows else "no watched button frames observed\n"))
    f.write("\n")

    f.write("[7] Latest state snapshots\n")
    for service in (
      "carState", "carControl", "carOutput", "controlsState", "selfdriveState",
      "longitudinalPlan", "radarState", "pandaStates", "carParams",
    ):
      if service not in latest_json:
        continue
      text = json.dumps(latest_json[service], ensure_ascii=False, indent=2, default=_json_default)
      if len(text) > 30000:
        text = text[:30000] + "\n... <snapshot truncated in report; full events.jsonl/raw_events.bin retained>"
      f.write(f"\n--- {service} ---\n{text}\n")
    f.write("\n")

    f.write("[8] rlog/qlog references overlapping the recording window\n")
    if route_refs:
      for item in route_refs:
        f.write(f"{item['path']} | {item['size']} bytes | {item['mtime_iso']}\n")
    else:
      f.write("matching rlog/qlog path not found\n")
    f.write("matching_rlog_qlog.json contains the machine-readable list.\n\n")

    f.write("[9] Files in the downloadable package\n")
    f.write("raw_events.bin  : exact raw cereal Event bytes + receive timestamp/service index\n")
    f.write("events.jsonl    : decoded state/control/radar/Panda/CarParams messages\n")
    f.write("can_rx.csv      : every received raw CAN frame\n")
    f.write("sendcan_tx.csv  : every openpilot sendcan frame\n")
    f.write("manifest.json   : service map, code identity, timestamps and format metadata\n")
    f.write("params_start/end.json + CarParams_start/end.bin\n")
    f.write("git_status.txt + git_diff.patch + source_hits.txt\n")
    f.write("matching_rlog_qlog.json\n\n")

    if worker_errors:
      f.write("[10] Recorder warnings/errors\n")
      for error in worker_errors:
        f.write(error.rstrip() + "\n")
      f.write("\n")

    f.write("NEXO_LONG_LOG_COMPLETE\n")

  return report_path


def _build_archive(session_dir: str, session: str) -> str:
  os.makedirs(os.path.dirname(LATEST_ARCHIVE), exist_ok=True)
  tmp = f"{LATEST_ARCHIVE}.{os.getpid()}.tmp"
  try:
    os.remove(tmp)
  except FileNotFoundError:
    pass
  with tarfile.open(tmp, "w:gz") as tar:
    tar.add(session_dir, arcname=f"nexo-long-log-{session}", recursive=True)
  os.replace(tmp, LATEST_ARCHIVE)
  return LATEST_ARCHIVE


def _capture_frame_csv(
  writer: csv.writer,
  direction: str,
  recv_ns: int,
  log_mono_time: int,
  frames: Any,
  frame_counts: Counter,
) -> None:
  for frame in list(frames):
    try:
      bus = int(_safe(frame, "src", -1))
      address = int(_safe(frame, "address", -1))
      dat = bytes(_safe(frame, "dat", b""))
      frame_counts[(direction, bus, address)] += 1
      writer.writerow([
        recv_ns,
        log_mono_time,
        bus,
        f"0x{address:X}",
        address,
        dat.hex().upper(),
      ])
    except Exception:
      continue


def _worker_main(session: str, session_dir: str, start_epoch: float, start_mono: float) -> None:
  worker_errors: list[str] = []
  services = [s for s in DESIRED_SERVICES if s in SERVICE_LIST]
  missing_services = [s for s in DESIRED_SERVICES if s not in SERVICE_LIST]
  service_index = {service: i for i, service in enumerate(services)}
  service_counts: Counter = Counter()
  frame_counts: Counter = Counter()
  latest_json: dict[str, Any] = {}

  # Subscribe first so the beginning of the user's drive is not lost while
  # Git/Params/source metadata are being collected.
  poller = messaging.Poller()
  sockets: dict[str, Any] = {}
  socket_service: dict[int, str] = {}
  try:
    for service in services:
      sock = messaging.sub_sock(service, poller=poller, conflate=False)
      sockets[service] = sock
      socket_service[id(sock)] = service
  except Exception:
    worker_errors.append("socket setup failed:\n" + traceback.format_exc())

  git_info = _git_snapshot(session_dir)
  _param_snapshot(session_dir, "start")

  manifest_path = os.path.join(session_dir, "manifest.json")
  raw_path = os.path.join(session_dir, "raw_events.bin")
  jsonl_path = os.path.join(session_dir, "events.jsonl")
  can_path = os.path.join(session_dir, "can_rx.csv")
  sendcan_path = os.path.join(session_dir, "sendcan_tx.csv")

  manifest = {
    "format": "NEXO_LONG_LOG_V1",
    "session": session,
    "started_at_epoch": start_epoch,
    "started_at_iso": datetime.fromtimestamp(start_epoch).isoformat(timespec="seconds"),
    "git": git_info,
    "services": services,
    "service_index": service_index,
    "missing_services": missing_services,
    "raw_event_record_format": {
      "magic": "NXLPLOG1\\n",
      "record_header_big_endian": ">QHI",
      "fields": ["receive_monotonic_ns_uint64", "service_index_uint16", "payload_length_uint32"],
      "payload": "exact cereal log.Event bytes",
    },
    "camera_video_included": False,
  }
  _atomic_json(manifest_path, manifest)

  def scan_source() -> None:
    try:
      _source_scan(session_dir)
    except Exception:
      worker_errors.append("source scan failed:\n" + traceback.format_exc())

  # Source evidence can be relatively expensive to scan. Do it in parallel so
  # CAN/state recording starts immediately.
  source_thread = threading.Thread(target=scan_source, name="nexo-source-scan", daemon=True)
  source_thread.start()

  last_flush = time.monotonic()
  try:
    with (
      open(raw_path, "wb") as raw_file,
      open(jsonl_path, "w", encoding="utf-8") as jsonl_file,
      open(can_path, "w", encoding="utf-8", newline="") as can_file,
      open(sendcan_path, "w", encoding="utf-8", newline="") as sendcan_file,
    ):
      raw_file.write(_MAGIC)
      can_writer = csv.writer(can_file)
      sendcan_writer = csv.writer(sendcan_file)
      header = ["receiveMonoNs", "logMonoTime", "bus", "addressHex", "addressDec", "dataHex"]
      can_writer.writerow(header)
      sendcan_writer.writerow(header)

      while True:
        should_stop = _stop_event is not None and _stop_event.is_set()
        ready = poller.poll(100)

        for sock in ready:
          service = socket_service.get(id(sock))
          if service is None:
            continue
          # Drain every queued message so raw CAN/sendcan are not conflated.
          while True:
            raw = sock.receive(non_blocking=True)
            if raw is None:
              break
            recv_ns = time.monotonic_ns()
            service_counts[service] += 1
            raw_file.write(_RECORD.pack(recv_ns, service_index[service], len(raw)))
            raw_file.write(raw)

            try:
              with log.Event.from_bytes(raw, traversal_limit_in_words=messaging.NO_TRAVERSAL_LIMIT) as msg:
                actual_service = msg.which()
                log_mono_time = int(msg.logMonoTime)
                payload = getattr(msg, actual_service)

                if actual_service == "can":
                  _capture_frame_csv(can_writer, "RX", recv_ns, log_mono_time, payload, frame_counts)
                elif actual_service == "sendcan":
                  _capture_frame_csv(sendcan_writer, "TX", recv_ns, log_mono_time, payload, frame_counts)
                else:
                  decoded = _payload_to_json(payload)
                  latest_json[actual_service] = decoded
                  record = {
                    "receiveMonoNs": recv_ns,
                    "logMonoTime": log_mono_time,
                    "valid": bool(msg.valid),
                    "service": actual_service,
                    "data": decoded,
                  }
                  jsonl_file.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
            except Exception as e:
              if len(worker_errors) < 50:
                worker_errors.append(f"decode {service}: {type(e).__name__}: {e}")

        now = time.monotonic()
        if now - last_flush >= 1.0:
          raw_file.flush()
          jsonl_file.flush()
          can_file.flush()
          sendcan_file.flush()
          with _lock:
            _state["elapsed"] = max(0.0, now - start_mono)
          _persist_state()
          last_flush = now

        if should_stop:
          # One final non-blocking drain happens on this pass before exit.
          break

      raw_file.flush()
      os.fsync(raw_file.fileno())
      jsonl_file.flush()
      os.fsync(jsonl_file.fileno())
      can_file.flush()
      os.fsync(can_file.fileno())
      sendcan_file.flush()
      os.fsync(sendcan_file.fileno())

  except Exception:
    worker_errors.append("recorder loop failed:\n" + traceback.format_exc())

  source_thread.join(timeout=30.0)
  if source_thread.is_alive():
    worker_errors.append("source scan is still running; source_hits.txt may be incomplete")

  stop_epoch = time.time()
  _param_snapshot(session_dir, "end")
  route_refs = _route_log_references(start_epoch, stop_epoch, session_dir)

  manifest.update({
    "stopped_at_epoch": stop_epoch,
    "stopped_at_iso": datetime.fromtimestamp(stop_epoch).isoformat(timespec="seconds"),
    "elapsed_seconds": max(0.0, stop_epoch - start_epoch),
    "service_counts": dict(service_counts),
    "errors": worker_errors,
    "matching_rlog_qlog_count": len(route_refs),
  })
  _atomic_json(manifest_path, manifest)

  report_path = _write_report(
    session_dir=session_dir,
    session=session,
    start_epoch=start_epoch,
    stop_epoch=stop_epoch,
    git_info=git_info,
    services=services,
    missing_services=missing_services,
    service_counts=service_counts,
    frame_counts=frame_counts,
    latest_json=latest_json,
    worker_errors=worker_errors,
    route_refs=route_refs,
  )

  archive_path = None
  try:
    archive_path = _build_archive(session_dir, session)
  except Exception:
    worker_errors.append("archive build failed:\n" + traceback.format_exc())

  with _lock:
    _state.update({
      "active": False,
      "finalizing": False,
      "finished": True,
      "stopped_at": stop_epoch,
      "elapsed": max(0.0, stop_epoch - start_epoch),
      "report_path": report_path,
      "archive_path": archive_path,
      "error": "\n".join(worker_errors[-5:]) if worker_errors else None,
    })
  _persist_state()


def start() -> dict[str, Any]:
  global _stop_event, _worker
  with _lock:
    if _state.get("active") or _state.get("finalizing"):
      return {"ok": False, "error": "이미 장시간 로그를 기록 중입니다.", **status()}

    session = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = os.path.join(BASE_DIR, session)
    os.makedirs(session_dir, exist_ok=False)
    start_epoch = time.time()
    start_mono = time.monotonic()
    _stop_event = threading.Event()
    _state.update({
      "active": True,
      "finalizing": False,
      "finished": False,
      "session": session,
      "session_dir": session_dir,
      "started_at": start_epoch,
      "started_mono": start_mono,
      "stopped_at": None,
      "elapsed": 0.0,
      "report_path": None,
      "archive_path": None,
      "error": None,
    })
    _worker = threading.Thread(
      target=_worker_main,
      args=(session, session_dir, start_epoch, start_mono),
      name="nexo-long-logger",
      daemon=True,
    )
    _worker.start()
    _persist_state()
    return {"ok": True, **status()}


def stop(timeout: float = 45.0) -> dict[str, Any]:
  global _worker
  with _lock:
    if not _state.get("active"):
      if _state.get("finished") and _state.get("report_path"):
        return {"ok": True, **status()}
      return {"ok": False, "error": "현재 기록 중인 장시간 로그가 없습니다.", **status()}
    _state["finalizing"] = True
    if _stop_event is not None:
      _stop_event.set()
    worker = _worker

  if worker is not None:
    worker.join(timeout=timeout)

  with _lock:
    if worker is not None and worker.is_alive():
      _state["error"] = "기록 종료 처리가 아직 진행 중입니다."
      _persist_state()
      return {"ok": False, "processing": True, **status()}
    return {"ok": bool(_state.get("finished")), **status()}


def status() -> dict[str, Any]:
  with _lock:
    result = dict(_state)
  if result.get("active") and result.get("started_mono") is not None:
    result["elapsed"] = max(0.0, time.monotonic() - float(result["started_mono"]))
  result.pop("started_mono", None)
  result.pop("session_dir", None)
  result["result_url"] = "/view/nexo-long-log-report.txt" if result.get("report_path") else None
  result["download_url"] = "/download/nexo-long-log-latest.tar.gz" if result.get("archive_path") else None
  return result


def report_path() -> str | None:
  with _lock:
    path = _state.get("report_path")
  return str(path) if path else None


def archive_path() -> str | None:
  with _lock:
    path = _state.get("archive_path")
  return str(path) if path else None


_load_state()
