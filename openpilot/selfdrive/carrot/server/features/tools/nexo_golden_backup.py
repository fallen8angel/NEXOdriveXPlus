from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openpilot.cereal import car, messaging
from openpilot.common.params import Params


REPO_ROOT = Path("/data/openpilot")
BASE_DIR = Path("/data/media/nexo-xplus-golden")
LATEST_ARCHIVE = Path("/data/media/nexo-xplus-golden-backup.tar.gz")
LATEST_MANIFEST = Path("/data/media/nexo-xplus-golden-manifest.txt")
LOG_ROOTS = (Path("/data/media/0/realdata"), Path("/data/media/0"))

MAX_COPY_FILE_BYTES = 64 * 1024 * 1024
MAX_DIR_FILE_BYTES = 8 * 1024 * 1024
MAX_DIR_TOTAL_BYTES = 220 * 1024 * 1024
MAX_ROUTE_LOG_BYTES = 420 * 1024 * 1024
MAX_DIR_FILES = 15000

SECRET_PARAM_TOKENS = (
  "token", "secret", "password", "passwd", "auth", "cookie", "private",
  "credential", "jwt", "ssh", "wifi", "ssid", "apikey", "api_key",
)
SAFE_PARAM_TOKENS = (
  "car", "cruise", "long", "scc", "radar", "nexo", "hyundai", "carrot",
  "mad", "med", "safety", "steer", "torque", "button", "enable",
  "experimental", "speed", "controls", "firmware", "fca", "gap", "hold",
  "panda", "fingerprint", "openpilot", "drive", "mode",
)

SOURCE_ROOTS = (
  "opendbc_repo/opendbc/car/hyundai",
  "openpilot/selfdrive/car/hyundai",
  "panda/board/safety",
  "panda_repo/board/safety",
  "openpilot/selfdrive/controls",
  "openpilot/selfdrive/carrot",
)
SOURCE_FILES = (
  "sitecustomize.py",
  "nexo_ai_cruise.py",
  "openpilot/selfdrive/car/interfaces.py",
  "openpilot/selfdrive/selfdrived/selfdrived.py",
  "openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py",
  "openpilot/selfdrive/carrot/server/features/tools/nexo_long_detail_diag.py",
  "openpilot/selfdrive/carrot/server/features/tools/nexo_long_forensic_diag.py",
  "openpilot/selfdrive/carrot/server/features/tools/nexo_long_logger.py",
)
SOURCE_EXTS = {
  ".py", ".h", ".hpp", ".c", ".cc", ".cpp", ".dbc", ".json", ".toml", ".yaml",
  ".yml", ".txt", ".md", ".capnp",
}

RUNTIME_SERVICES = (
  "carState", "carControl", "carOutput", "controlsState", "selfdriveState",
  "longitudinalPlan", "radarState", "liveTracks", "pandaStates", "carParams",
  "onroadEvents", "managerState", "deviceState", "liveParameters",
  "liveTorqueParameters", "liveCalibration",
)

_lock = threading.RLock()
_worker: threading.Thread | None = None
_state: dict[str, Any] = {
  "active": False,
  "finished": False,
  "session": None,
  "started_at": None,
  "finished_at": None,
  "progress": 0,
  "message": "",
  "archive_path": None,
  "manifest_path": None,
  "archive_size": 0,
  "archive_sha256": None,
  "error": None,
}


def _set_state(**kwargs: Any) -> None:
  with _lock:
    _state.update(kwargs)


def status() -> dict[str, Any]:
  with _lock:
    snapshot = dict(_state)
  if not snapshot.get("active") and not snapshot.get("finished") and LATEST_ARCHIVE.is_file():
    try:
      snapshot.update({
        "finished": True,
        "message": "기존 골든 백업 다운로드 가능",
        "archive_path": str(LATEST_ARCHIVE),
        "manifest_path": str(LATEST_MANIFEST) if LATEST_MANIFEST.is_file() else None,
        "archive_size": LATEST_ARCHIVE.stat().st_size,
      })
    except Exception:
      pass
  return snapshot


def archive_path() -> str | None:
  with _lock:
    path = _state.get("archive_path")
  if path:
    return str(path)
  return str(LATEST_ARCHIVE) if LATEST_ARCHIVE.is_file() else None


def manifest_path() -> str | None:
  with _lock:
    path = _state.get("manifest_path")
  if path:
    return str(path)
  return str(LATEST_MANIFEST) if LATEST_MANIFEST.is_file() else None


def _run(args: list[str], timeout: float = 8.0) -> str:
  try:
    p = subprocess.run(
      args,
      cwd=REPO_ROOT,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      timeout=timeout,
      check=False,
    )
    out = (p.stdout or "").rstrip()
    if p.returncode != 0:
      return f"[exit={p.returncode}] {out}".rstrip()
    return out
  except Exception as e:
    return f"[{type(e).__name__}] {e}"


def _write_text(path: Path, text: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
  with open(tmp, "w", encoding="utf-8") as f:
    f.write(text)
    f.flush()
    os.fsync(f.fileno())
  os.replace(tmp, path)


def _write_json(path: Path, value: Any) -> None:
  _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _hash_file(path: Path) -> str:
  h = hashlib.sha256()
  with open(path, "rb") as f:
    while True:
      chunk = f.read(1024 * 1024)
      if not chunk:
        break
      h.update(chunk)
  return h.hexdigest()


def _copy_file(src: Path, dst: Path, *, max_bytes: int = MAX_COPY_FILE_BYTES) -> dict[str, Any]:
  item: dict[str, Any] = {"src": str(src), "dst": str(dst)}
  try:
    st = src.stat()
    item["size"] = st.st_size
    item["mtime"] = st.st_mtime
    if st.st_size > max_bytes:
      item["copied"] = False
      item["reason"] = f"size>{max_bytes}"
      item["sha256"] = _hash_file(src)
      return item
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    item["copied"] = True
    item["sha256"] = _hash_file(dst)
  except Exception as e:
    item["copied"] = False
    item["reason"] = f"{type(e).__name__}: {e}"
  return item


def _copy_git_dirty_files(repo_root: Path, status_text: str, session_dir: Path, prefix: str = "") -> list[dict[str, Any]]:
  results: list[dict[str, Any]] = []
  for line in status_text.splitlines():
    if len(line) < 4:
      continue
    rel_text = line[3:]
    if " -> " in rel_text:
      rel_text = rel_text.split(" -> ", 1)[1]
    rel_text = rel_text.strip().strip('"')
    src = repo_root / rel_text
    if not src.is_file():
      continue
    dst_rel = Path(prefix) / rel_text if prefix else Path(rel_text)
    results.append(_copy_file(src, session_dir / "SOURCE_DIRTY" / dst_rel))
  return results


def _copy_source_tree(session_dir: Path) -> list[dict[str, Any]]:
  source_out = session_dir / "SOURCE"
  results: list[dict[str, Any]] = []
  copied_bytes = 0
  copied_files = 0

  candidates: list[Path] = []
  for rel in SOURCE_FILES:
    p = REPO_ROOT / rel
    if p.is_file():
      candidates.append(p)

  for rel in SOURCE_ROOTS:
    root = REPO_ROOT / rel
    if not root.is_dir():
      continue
    for dirpath, dirnames, filenames in os.walk(root):
      dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "build", "generated")]
      for name in filenames:
        p = Path(dirpath) / name
        if p.suffix.lower() not in SOURCE_EXTS:
          continue
        candidates.append(p)

  # Include Hyundai/Kia/NEXO DBCs even when they live outside the active car directory.
  for dbc_root_rel in ("opendbc_repo/opendbc/dbc", "opendbc/dbc"):
    dbc_root = REPO_ROOT / dbc_root_rel
    if not dbc_root.is_dir():
      continue
    for p in dbc_root.rglob("*.dbc"):
      lower = p.name.lower()
      if "hyundai" in lower or "kia" in lower or "nexo" in lower:
        candidates.append(p)

  seen: set[str] = set()
  for src in candidates:
    try:
      rel = src.relative_to(REPO_ROOT)
    except Exception:
      continue
    key = str(rel)
    if key in seen:
      continue
    seen.add(key)
    if copied_files >= MAX_DIR_FILES or copied_bytes >= MAX_DIR_TOTAL_BYTES:
      results.append({"src": str(src), "copied": False, "reason": "source capture total limit reached"})
      continue
    try:
      size = src.stat().st_size
    except Exception:
      size = 0
    if size > MAX_DIR_FILE_BYTES:
      results.append({
        "src": str(src), "size": size, "copied": False,
        "reason": f"source file size>{MAX_DIR_FILE_BYTES}",
        "sha256": _hash_file(src) if src.is_file() else None,
      })
      continue
    result = _copy_file(src, source_out / rel, max_bytes=MAX_DIR_FILE_BYTES)
    results.append(result)
    if result.get("copied"):
      copied_files += 1
      copied_bytes += int(result.get("size") or 0)

  return results


def _git_snapshot(session_dir: Path) -> dict[str, Any]:
  git_dir = session_dir / "GIT"
  git_dir.mkdir(parents=True, exist_ok=True)

  values = {
    "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
    "head": _run(["git", "rev-parse", "HEAD"]),
    "remote": _run(["git", "remote", "-v"]),
    "status": _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], timeout=15.0),
    "submodules": _run(["git", "submodule", "status", "--recursive"], timeout=15.0),
    "recent_commits": _run(["git", "log", "-20", "--date=iso", "--pretty=format:%H %ad %s"], timeout=10.0),
  }
  _write_text(git_dir / "branch.txt", values["branch"] + "\n")
  _write_text(git_dir / "head.txt", values["head"] + "\n")
  _write_text(git_dir / "remote.txt", values["remote"] + "\n")
  _write_text(git_dir / "status.txt", values["status"] + "\n")
  _write_text(git_dir / "submodules.txt", values["submodules"] + "\n")
  _write_text(git_dir / "recent_commits.txt", values["recent_commits"] + "\n")

  diff = _run(["git", "diff", "--binary", "--no-ext-diff"], timeout=30.0)
  staged = _run(["git", "diff", "--cached", "--binary", "--no-ext-diff"], timeout=30.0)
  _write_text(git_dir / "working_tree.patch", diff + "\n")
  _write_text(git_dir / "staged.patch", staged + "\n")
  values["dirty"] = bool(values["status"].strip())
  values["diff_sha256"] = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()
  values["staged_diff_sha256"] = hashlib.sha256(staged.encode("utf-8", errors="replace")).hexdigest()

  # Preserve exact working-tree versions of every modified/untracked file.
  dirty_results = _copy_git_dirty_files(REPO_ROOT, values["status"], session_dir)

  # The active Hyundai code commonly lives in git submodules. Top-level git diff
  # only reports a dirty submodule pointer, so capture each submodule's own HEAD,
  # status, diff, staged diff, and exact dirty files as well.
  submodule_results: list[dict[str, Any]] = []
  for line in values["submodules"].splitlines():
    parts = line.strip().lstrip("-+U").split()
    if len(parts) < 2:
      continue
    rel = parts[1]
    repo = REPO_ROOT / rel
    if not repo.is_dir():
      continue
    safe_name = rel.replace("/", "__")
    sub_out = git_dir / "submodules" / safe_name
    sub_out.mkdir(parents=True, exist_ok=True)
    sub_head = _run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    sub_branch = _run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
    sub_status = _run(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"], timeout=15.0)
    sub_diff = _run(["git", "-C", str(repo), "diff", "--binary", "--no-ext-diff"], timeout=30.0)
    sub_staged = _run(["git", "-C", str(repo), "diff", "--cached", "--binary", "--no-ext-diff"], timeout=30.0)
    _write_text(sub_out / "head.txt", sub_head + "\n")
    _write_text(sub_out / "branch.txt", sub_branch + "\n")
    _write_text(sub_out / "status.txt", sub_status + "\n")
    _write_text(sub_out / "working_tree.patch", sub_diff + "\n")
    _write_text(sub_out / "staged.patch", sub_staged + "\n")
    sub_dirty = _copy_git_dirty_files(repo, sub_status, session_dir, prefix=rel)
    dirty_results.extend(sub_dirty)
    submodule_results.append({
      "path": rel,
      "head": sub_head,
      "branch": sub_branch,
      "dirty": bool(sub_status.strip()),
      "diff_sha256": hashlib.sha256(sub_diff.encode("utf-8", errors="replace")).hexdigest(),
      "staged_diff_sha256": hashlib.sha256(sub_staged.encode("utf-8", errors="replace")).hexdigest(),
      "dirty_file_count": len(sub_dirty),
    })

  _write_json(git_dir / "dirty_files.json", dirty_results)
  _write_json(git_dir / "submodule_snapshots.json", submodule_results)
  values["submodule_snapshots"] = submodule_results
  return values


def _param_snapshot(session_dir: Path) -> dict[str, Any]:
  out_dir = session_dir / "PARAMS"
  out_dir.mkdir(parents=True, exist_ok=True)
  params_root = Path("/data/params/d")
  inventory: list[dict[str, Any]] = []
  selected: dict[str, Any] = {}

  try:
    names = sorted(os.listdir(params_root))
  except Exception:
    names = []

  for name in names:
    path = params_root / name
    if not path.is_file():
      continue
    lower = name.lower()
    try:
      size = path.stat().st_size
    except Exception:
      size = -1
    item: dict[str, Any] = {"name": name, "size": size}
    if any(token in lower for token in SECRET_PARAM_TOKENS):
      item["value"] = "<redacted>"
      inventory.append(item)
      continue

    if any(token in lower for token in SAFE_PARAM_TOKENS):
      try:
        raw = path.read_bytes()
        if len(raw) <= 256 * 1024:
          try:
            value: Any = raw.decode("utf-8")
          except UnicodeDecodeError:
            value = {"binary_hex": raw.hex().upper()}
          selected[name] = value
          item["captured"] = True
        else:
          item["captured"] = False
          item["reason"] = "value larger than 256 KiB"
          item["sha256"] = _hash_file(path)
      except Exception as e:
        item["error"] = f"{type(e).__name__}: {e}"
    inventory.append(item)

  _write_json(out_dir / "inventory.json", inventory)
  _write_json(out_dir / "selected_vehicle_values.json", selected)

  result: dict[str, Any] = {"selected_count": len(selected), "inventory_count": len(inventory)}
  params = Params()
  for key in ("CarParams", "CarParamsPersistent"):
    try:
      raw = params.get(key)
      if not raw:
        continue
      (out_dir / f"{key}.bin").write_bytes(raw)
      result[f"{key}_size"] = len(raw)
      try:
        with car.CarParams.from_bytes(raw) as cp:
          cp_dict = cp.to_dict()
        _write_json(out_dir / f"{key}.json", cp_dict)
        result[f"{key}_decoded"] = True
      except Exception as e:
        result[f"{key}_decode_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
      result[f"{key}_error"] = f"{type(e).__name__}: {e}"
  return result


def _json_safe(value: Any) -> Any:
  if isinstance(value, (str, int, float, bool)) or value is None:
    return value
  if isinstance(value, (bytes, bytearray, memoryview)):
    return {"__bytes_hex": bytes(value).hex().upper()}
  if isinstance(value, dict):
    return {str(k): _json_safe(v) for k, v in value.items()}
  try:
    if hasattr(value, "to_dict"):
      return _json_safe(value.to_dict())
  except Exception:
    pass
  try:
    return [_json_safe(v) for v in value]
  except Exception:
    return str(value)


def _runtime_snapshot(session_dir: Path) -> dict[str, Any]:
  out_dir = session_dir / "RUNTIME"
  out_dir.mkdir(parents=True, exist_ok=True)
  available = []
  try:
    from openpilot.cereal.services import SERVICE_LIST
    available = [name for name in RUNTIME_SERVICES if name in SERVICE_LIST]
  except Exception:
    available = list(RUNTIME_SERVICES)

  result: dict[str, Any] = {}
  try:
    sm = messaging.SubMaster(available)
    deadline = time.monotonic() + 2.0
    seen: set[str] = set()
    while time.monotonic() < deadline and len(seen) < len(available):
      sm.update(100)
      for name in available:
        try:
          if sm.updated[name]:
            seen.add(name)
            result[name] = _json_safe(sm[name])
        except Exception:
          continue
  except Exception as e:
    result["_error"] = f"{type(e).__name__}: {e}"

  _write_json(out_dir / "latest_services.json", result)
  return {"available": available, "captured": sorted(k for k in result if not k.startswith("_"))}


def _parse_segment_dir(name: str) -> tuple[str, int | None]:
  parts = name.rsplit("--", 1)
  if len(parts) != 2:
    return name, None
  try:
    return parts[0], int(parts[1])
  except ValueError:
    return name, None


def _route_log_candidates() -> tuple[list[Path], list[dict[str, Any]]]:
  files: list[Path] = []
  seen: set[str] = set()
  refs: list[dict[str, Any]] = []

  for root in LOG_ROOTS:
    if not root.is_dir():
      continue
    for dirpath, dirnames, filenames in os.walk(root):
      if root == Path("/data/media/0"):
        dirnames[:] = [
          d for d in dirnames
          if d not in ("dcam", "fcam", "ecam", "video", "videos", "screenrecord", "nexo-xplus-golden", "nexo-long-logs")
        ]
      for name in filenames:
        lower = name.lower()
        if lower not in ("rlog", "rlog.zst", "qlog", "qlog.zst"):
          continue
        p = Path(dirpath) / name
        real = str(p.resolve())
        if real in seen:
          continue
        seen.add(real)
        try:
          st = p.stat()
        except Exception:
          continue
        route, segment = _parse_segment_dir(p.parent.name)
        refs.append({
          "path": str(p),
          "route": route,
          "segment": segment,
          "size": st.st_size,
          "mtime": st.st_mtime,
          "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })

  if not refs:
    return files, refs

  route_mtime: dict[str, float] = {}
  for item in refs:
    route = str(item["route"])
    route_mtime[route] = max(route_mtime.get(route, 0.0), float(item["mtime"]))
  selected_routes = [r for r, _ in sorted(route_mtime.items(), key=lambda kv: kv[1], reverse=True)[:2]]

  selected_refs: list[dict[str, Any]] = []
  for route in selected_routes:
    group = [item for item in refs if item["route"] == route]
    segs = sorted({int(item["segment"]) for item in group if item["segment"] is not None})
    wanted_segments: set[int | None] = set()
    if segs:
      wanted_segments.add(segs[0])
      wanted_segments.add(segs[-1])
    else:
      wanted_segments.add(None)

    for item in sorted(group, key=lambda x: (x["segment"] is None, x["segment"] or 0, x["path"])):
      if item["segment"] in wanted_segments:
        selected_refs.append(item)

  # Cap total bytes while always preferring rlog before qlog for each chosen segment.
  selected_refs.sort(key=lambda x: (
    0 if "rlog" in Path(str(x["path"])).name.lower() else 1,
    -float(x["mtime"]),
  ))
  total = 0
  final_refs: list[dict[str, Any]] = []
  for item in selected_refs:
    size = int(item["size"])
    if total + size > MAX_ROUTE_LOG_BYTES:
      item = dict(item)
      item["bundled"] = False
      item["reason"] = "route log total size limit"
      final_refs.append(item)
      continue
    item = dict(item)
    item["bundled"] = True
    final_refs.append(item)
    total += size

  paths = [Path(str(item["path"])) for item in final_refs if item.get("bundled")]
  return paths, final_refs


def _copy_route_logs(session_dir: Path) -> list[dict[str, Any]]:
  paths, refs = _route_log_candidates()
  out = session_dir / "ROUTE_LOGS"
  path_map = {str(item["path"]): item for item in refs}
  for idx, src in enumerate(paths):
    item = path_map[str(src)]
    route = str(item["route"]).replace("/", "_")
    segment = item["segment"]
    seg_text = "unknown" if segment is None else str(segment)
    dst = out / route / f"segment-{seg_text}" / src.name
    result = _copy_file(src, dst, max_bytes=MAX_ROUTE_LOG_BYTES)
    item["copy_result"] = result
  _write_json(session_dir / "ROUTE_LOGS" / "selected_rlog_qlog.json", refs)
  return refs


def _copy_existing_diagnostics(session_dir: Path) -> list[dict[str, Any]]:
  results = []
  candidates = (
    Path("/data/media/nexo-8sec-diagnostic.txt"),
    Path("/data/media/nexo-long-log-latest.tar.gz"),
  )
  for src in candidates:
    if not src.is_file():
      continue
    # Avoid nesting a very large long-log archive. Preserve it only when small enough;
    # otherwise keep its hash/metadata so it can be matched later.
    max_bytes = 96 * 1024 * 1024 if src.name.endswith(".tar.gz") else MAX_COPY_FILE_BYTES
    results.append(_copy_file(src, session_dir / "DIAGNOSTICS" / src.name, max_bytes=max_bytes))
  return results


def _system_snapshot(session_dir: Path) -> dict[str, Any]:
  out = {
    "captured_at": datetime.now().isoformat(timespec="seconds"),
    "uname": _run(["uname", "-a"]),
    "python": _run(["python3", "--version"]),
    "df": _run(["df", "-h", "/data", "/data/media"]),
  }
  _write_json(session_dir / "SYSTEM" / "system.json", out)
  return out


def _write_sha256_manifest(session_dir: Path) -> tuple[int, int]:
  rows = []
  total = 0
  count = 0
  for p in sorted(session_dir.rglob("*")):
    if not p.is_file() or p.name == "SHA256SUMS.txt":
      continue
    try:
      rel = p.relative_to(session_dir)
      size = p.stat().st_size
      rows.append(f"{_hash_file(p)}  {rel}  {size}")
      count += 1
      total += size
    except Exception as e:
      rows.append(f"ERROR  {p}  {type(e).__name__}: {e}")
  _write_text(session_dir / "SHA256SUMS.txt", "\n".join(rows) + "\n")
  return count, total


def _build_manifest_text(session: str, summary: dict[str, Any]) -> str:
  git = summary.get("git", {})
  params = summary.get("params", {})
  runtime = summary.get("runtime", {})
  route_refs = summary.get("route_logs", [])
  lines = [
    "=" * 78,
    "NEXOdriveXPlus -> NexoPilot 골든 레퍼런스 백업",
    "=" * 78,
    f"session: {session}",
    f"captured_at: {summary.get('captured_at', '-')}",
    "",
    "[1] 실행 XPlus 코드 정체성",
    f"branch: {git.get('branch', '-')}",
    f"HEAD: {git.get('head', '-')}",
    f"dirty: {git.get('dirty', '-')}",
    f"working diff sha256: {git.get('diff_sha256', '-')}",
    f"staged diff sha256: {git.get('staged_diff_sha256', '-')}",
    "GIT/working_tree.patch + staged.patch + SOURCE_DIRTY/에 실제 로컬 변경본을 보존합니다.",
    "",
    "[2] NEXO/Hyundai 실제 소스",
    "SOURCE/에 Hyundai carcontroller/carstate/interface/hyundaican/radar/values, Carrot MED/롱컨 코드, Panda safety, Hyundai/Kia/NEXO DBC를 보존합니다.",
    "",
    "[3] 차량 설정/CarParams",
    f"selected params: {params.get('selected_count', 0)} / inventory: {params.get('inventory_count', 0)}",
    "PARAMS/CarParams.bin/json 및 CarParamsPersistent가 존재하면 함께 보존합니다.",
    "토큰/비밀번호/인증정보 계열 Params 값은 백업하지 않고 <redacted> 처리합니다.",
    "",
    "[4] 런타임 상태",
    "captured services: " + ", ".join(runtime.get("captured", [])),
    "RUNTIME/latest_services.json에 Panda/CarState/CarControl/longitudinalPlan/radarState 등을 저장합니다.",
    "",
    "[5] 최근 rlog/qlog",
  ]
  if route_refs:
    for item in route_refs:
      lines.append(
        f"{item.get('route')} seg={item.get('segment')} {Path(str(item.get('path'))).name} "
        f"{item.get('size')} bytes bundled={item.get('bundled')}"
      )
    lines.append("최신 2개 route에서 시작 segment와 최신 segment를 우선 보존해 부팅 SCC takeover와 최근 주행을 같이 남깁니다.")
  else:
    lines.append("rlog/qlog 후보를 찾지 못했습니다.")
  lines += [
    "",
    "[6] 무결성",
    f"files: {summary.get('file_count', 0)}",
    f"uncompressed bytes: {summary.get('uncompressed_bytes', 0)}",
    "SHA256SUMS.txt로 백업 내부 파일을 검증할 수 있습니다.",
    "",
    "이 백업은 차량 제어를 변경하지 않는 읽기/복사 전용 수집기입니다.",
    "NEXO_XPLUS_GOLDEN_COMPLETE",
    "",
  ]
  return "\n".join(lines)


def _worker_main(session: str) -> None:
  session_dir = BASE_DIR / session
  tmp_archive = LATEST_ARCHIVE.with_name(LATEST_ARCHIVE.name + f".{os.getpid()}.tmp")
  try:
    if session_dir.exists():
      shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    _set_state(progress=5, message="Git/dirty 작업트리 보존 중")
    git_info = _git_snapshot(session_dir)

    _set_state(progress=20, message="NEXO/Hyundai 실제 소스 보존 중")
    source_files = _copy_source_tree(session_dir)
    _write_json(session_dir / "SOURCE" / "copy_manifest.json", source_files)

    _set_state(progress=42, message="CarParams/차량 설정 보존 중")
    params_info = _param_snapshot(session_dir)

    _set_state(progress=52, message="Panda/제어/레이더 런타임 상태 보존 중")
    runtime_info = _runtime_snapshot(session_dir)

    _set_state(progress=61, message="기존 진단자료 보존 중")
    diag_info = _copy_existing_diagnostics(session_dir)
    _write_json(session_dir / "DIAGNOSTICS" / "copy_manifest.json", diag_info)

    _set_state(progress=68, message="최근 rlog/qlog 시작·최신 구간 보존 중")
    route_refs = _copy_route_logs(session_dir)

    _set_state(progress=82, message="시스템/해시 목록 작성 중")
    system_info = _system_snapshot(session_dir)

    summary: dict[str, Any] = {
      "session": session,
      "captured_at": datetime.now().isoformat(timespec="seconds"),
      "git": git_info,
      "params": params_info,
      "runtime": runtime_info,
      "route_logs": route_refs,
      "system": system_info,
    }
    _write_json(session_dir / "manifest.json", summary)
    file_count, uncompressed_bytes = _write_sha256_manifest(session_dir)
    summary["file_count"] = file_count
    summary["uncompressed_bytes"] = uncompressed_bytes
    _write_json(session_dir / "manifest.json", summary)
    _write_sha256_manifest(session_dir)
    manifest_text = _build_manifest_text(session, summary)
    _write_text(session_dir / "MANIFEST.txt", manifest_text)
    _write_text(LATEST_MANIFEST, manifest_text)

    _set_state(progress=90, message="골든 백업 압축 중")
    try:
      tmp_archive.unlink()
    except FileNotFoundError:
      pass
    with tarfile.open(tmp_archive, "w:gz", compresslevel=1) as tar:
      tar.add(session_dir, arcname=f"XPlus-NEXO-GOLDEN-{session}", recursive=True)
    os.replace(tmp_archive, LATEST_ARCHIVE)

    archive_size = LATEST_ARCHIVE.stat().st_size
    archive_hash = _hash_file(LATEST_ARCHIVE)
    _set_state(
      active=False,
      finished=True,
      finished_at=time.time(),
      progress=100,
      message="골든 백업 완료",
      archive_path=str(LATEST_ARCHIVE),
      manifest_path=str(LATEST_MANIFEST),
      archive_size=archive_size,
      archive_sha256=archive_hash,
      error=None,
    )
  except Exception as e:
    try:
      tmp_archive.unlink()
    except Exception:
      pass
    _set_state(
      active=False,
      finished=False,
      finished_at=time.time(),
      message="골든 백업 실패",
      error=f"{type(e).__name__}: {e}",
    )


def start() -> dict[str, Any]:
  global _worker
  with _lock:
    if _state.get("active"):
      return {"ok": False, "error": "골든 백업이 이미 진행 중입니다.", **dict(_state)}

    session = datetime.now().strftime("%Y%m%d-%H%M%S")
    _state.update({
      "active": True,
      "finished": False,
      "session": session,
      "started_at": time.time(),
      "finished_at": None,
      "progress": 1,
      "message": "골든 백업 준비 중",
      "archive_path": None,
      "manifest_path": None,
      "archive_size": 0,
      "archive_sha256": None,
      "error": None,
    })
    _worker = threading.Thread(target=_worker_main, args=(session,), name="nexo-xplus-golden", daemon=True)
    _worker.start()
    return {"ok": True, **dict(_state)}
