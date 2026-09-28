"""XPlus startup-only Git recovery using only the Python standard library."""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import time

LOG_LIMIT_BYTES = 64 * 1024
INITIAL_DELAY_SECONDS = 5
RETRY_SECONDS = 30
GIT_INFO_TIMEOUT = 15
GIT_FETCH_TIMEOUT = 1800
STATUS_PATH = Path("/tmp/nexodrivexplus_startup_recovery_status.json")


def _write_status(state: str, detail: str = "", *, reason: str = "") -> None:
  payload = {"time": time.time(), "state": state, "detail": str(detail or "")[-4000:], "reason": str(reason or "")[-1000:]}
  tmp = STATUS_PATH.with_suffix(".tmp")
  try:
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS_PATH)
  except OSError:
    pass


def capture_startup_output(source, destination, log_path: Path) -> None:
  chunks: deque[bytes] = deque()
  size = 0
  try:
    log_path.write_bytes(b"")
  except OSError:
    pass
  while True:
    data = source.read(4096)
    if not data:
      break
    destination.write(data)
    destination.flush()
    chunks.append(data)
    size += len(data)
    while chunks and size > LOG_LIMIT_BYTES:
      size -= len(chunks.popleft())
    try:
      log_path.write_bytes(b"".join(chunks))
    except OSError:
      pass


def _git(repo: Path, *args: str, timeout: int = GIT_INFO_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
  env = os.environ.copy()
  env.setdefault("GIT_TERMINAL_PROMPT", "0")
  result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
  if check and result.returncode:
    detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
    raise RuntimeError(detail)
  return result


def _tracking_ref(repo: Path, branch: str) -> tuple[str, str, str]:
  remote = _git(repo, "config", "--get", f"branch.{branch}.remote", check=False).stdout.strip()
  merge_ref = _git(repo, "config", "--get", f"branch.{branch}.merge", check=False).stdout.strip()
  remote_branch = merge_ref.removeprefix("refs/heads/") if merge_ref.startswith("refs/heads/") else ""
  if remote and remote_branch:
    return remote, remote_branch, f"{remote}/{remote_branch}"

  upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).stdout.strip()
  if "/" in upstream:
    remote, remote_branch = upstream.split("/", 1)
    return remote, remote_branch, upstream

  remotes = _git(repo, "remote", check=False).stdout.split()
  remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "")
  if not remote:
    raise RuntimeError("No Git remote is configured.")
  return remote, branch, f"{remote}/{branch}"


def pull_current_branch(repo: Path) -> tuple[str, bool]:
  branch = _git(repo, "branch", "--show-current").stdout.strip()
  if not branch:
    raise RuntimeError("Detached HEAD: select a branch before recovery update.")

  dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
  if dirty:
    raise RuntimeError("Tracked local changes found; recovery will not reset or overwrite them.")

  before = _git(repo, "rev-parse", "HEAD").stdout.strip()
  remote, remote_branch, upstream = _tracking_ref(repo, branch)
  refspec = f"+refs/heads/{remote_branch}:refs/remotes/{remote}/{remote_branch}"
  _git(repo, "fetch", "--quiet", remote, refspec, timeout=GIT_FETCH_TIMEOUT)
  target = _git(repo, "rev-parse", upstream).stdout.strip()
  if target == before:
    return target, False

  if _git(repo, "merge-base", "--is-ancestor", before, target, check=False).returncode != 0:
    raise RuntimeError("Local branch diverged from upstream; recovery requires manual resolution.")

  _git(repo, "merge", "--ff-only", target, timeout=GIT_FETCH_TIMEOUT)
  after = _git(repo, "rev-parse", "HEAD").stdout.strip()
  if after != target:
    raise RuntimeError("Fast-forward did not reach the verified upstream commit.")
  return after, True


def reboot_device() -> None:
  if not (Path("/AGNOS").exists() or Path("/TICI").exists()):
    raise RuntimeError("Automatic reboot is disabled outside a comma device.")
  subprocess.run(["sudo", "-n", "reboot"], check=True, capture_output=True, timeout=20)


def recovery_loop(repo: Path, reason: str) -> None:
  _write_status("waiting", "initial update check pending", reason=reason)
  time.sleep(INITIAL_DELAY_SECONDS)
  pending_reboot = False
  updated_sha = ""
  while True:
    if pending_reboot:
      try:
        _write_status("rebooting", updated_sha[:12], reason=reason)
        reboot_device()
      except Exception as exc:
        _write_status("reboot_failed", str(exc), reason=reason)
      time.sleep(RETRY_SECONDS)
      continue

    try:
      _write_status("checking", "checking current branch for a safe fast-forward", reason=reason)
      target, changed = pull_current_branch(repo)
      if changed:
        updated_sha = target
        pending_reboot = True
        _write_status("updated", f"new commit {target[:12]} applied; reboot pending", reason=reason)
        continue
      _write_status("waiting", f"no newer commit ({target[:12]})", reason=reason)
    except Exception as exc:
      _write_status("update_failed", str(exc), reason=reason)
    time.sleep(RETRY_SECONDS)


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--capture-log")
  parser.add_argument("--repo", default="/data/openpilot")
  parser.add_argument("--reason", default="startup failure")
  parser.add_argument("--log", default="/tmp/nexodrivexplus_startup_failure.log")
  args = parser.parse_args()
  if args.capture_log:
    capture_startup_output(sys.stdin.buffer, sys.stdout.buffer, Path(args.capture_log))
    return 0
  recovery_loop(Path(args.repo), args.reason)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
