"""Fail-closed regression checks for Git's untracked-file conflict backup."""
from openpilot.selfdrive.carrot.server.services.safe_git_pull import (
  _UNTRACKED_OVERWRITE_RE, backup_untracked_merge_conflicts,
)


def test_real_git_error_is_parsed():
  output = ("error: The following untracked working tree files would be overwritten by merge:\n"
            "  openpilot/local-notes.txt\n"
            "Please move or remove them before you merge.\nAborting")
  match = _UNTRACKED_OVERWRITE_RE.search(output)
  assert match is not None
  assert match.group("paths").strip() == "openpilot/local-notes.txt"


def test_path_traversal_and_absolute_path_are_never_moved(tmp_path):
  outside = tmp_path / "outside.txt"
  outside.write_text("preserve")
  repo = tmp_path / "repo"
  repo.mkdir()
  local = repo / "local.txt"
  local.write_text("preserve")
  output = ("The following untracked working tree files would be overwritten by merge:\n"
            "  ../outside.txt\n  " + str(local) + "\n"
            "Please move or remove them before you merge.\nAborting")
  assert backup_untracked_merge_conflicts(str(repo), output) == ([], "")
  assert outside.read_text() == "preserve"
  assert local.read_text() == "preserve"
