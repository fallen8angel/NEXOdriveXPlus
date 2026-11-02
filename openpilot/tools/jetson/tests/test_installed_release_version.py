"""Protected Jetson installed version is visible to the 7000 update checker."""
from openpilot.tools.jetson.manage import installed_source_commit


def test_version_from_nexo_prefixed_release(tmp_path):
  commit = 'a' * 40
  release = tmp_path / 'releases' / ('nexo-' + commit)
  release.mkdir(parents=True)
  (tmp_path / 'current').symlink_to(release, target_is_directory=True)
  assert installed_source_commit(tmp_path) == commit


def test_source_commit_marker_takes_precedence(tmp_path):
  commit = 'b' * 40
  release = tmp_path / 'releases' / ('nexo-' + 'a' * 40)
  release.mkdir(parents=True)
  (release / 'SOURCE_COMMIT').write_text(commit + '\n')
  (tmp_path / 'current').symlink_to(release, target_is_directory=True)
  assert installed_source_commit(tmp_path) == commit


def test_unknown_or_bad_release_is_not_assumed_current(tmp_path):
  release = tmp_path / 'releases' / 'broken'
  release.mkdir(parents=True)
  (release / 'SOURCE_COMMIT').write_text('not-a-commit')
  (tmp_path / 'current').symlink_to(release, target_is_directory=True)
  assert installed_source_commit(tmp_path) == ''
