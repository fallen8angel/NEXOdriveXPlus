from __future__ import annotations

import os
import re
import shutil
import time
from typing import List, Tuple


_UNTRACKED_OVERWRITE_RE = re.compile(
  r"The following untracked working tree files would be overwritten by merge:\\s*\\n(?P<paths>.*?)(?:\\nPlease move or remove them before you merge\\.|\\nAborting)",
  re.DOTALL,
)


def backup_untracked_merge_conflicts(repo_dir: str, git_output: str) -> Tuple[List[str], str]:
  """Move only untracked files that Git says would be overwritten by a merge.

  This intentionally does not run git clean. Files are preserved under /data so
  a failed update cannot silently destroy device-local work.
  """
  match = _UNTRACKED_OVERWRITE_RE.search(str(git_output or ""))
  if match is None:
    return [], ""

  repo_real = os.path.realpath(repo_dir)
  candidates = [line.strip() for line in match.group("paths").splitlines() if line.strip()]
  safe_paths: List[Tuple[str, str]] = []
  for rel in candidates:
    src = os.path.realpath(os.path.join(repo_real, rel))
    try:
      inside_repo = os.path.commonpath([repo_real, src]) == repo_real
    except ValueError:
      inside_repo = False
    if not inside_repo or not os.path.isfile(src):
      continue
    safe_paths.append((rel, src))

  if not safe_paths:
    return [], ""

  backup_root = os.path.join("/data", "git-update-backup", time.strftime("%Y%m%d-%H%M%S"))
  moved: List[str] = []
  for rel, src in safe_paths:
    dst = os.path.join(backup_root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    moved.append(rel)
  return moved, backup_root
