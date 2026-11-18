"""Execute source selection and immutable release guards without GitHub writes."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import textwrap
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / '.github/workflows/hud-recovery.yaml'


def block(name):
  lines = WORKFLOW.read_text().splitlines()
  start = next(i for i, line in enumerate(lines) if line.strip() == '- name: ' + name)
  run = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == 'run: |')
  indent = len(lines[run]) - len(lines[run].lstrip())
  content = []
  for line in lines[run + 1:]:
    if line.strip() and len(line) - len(line.lstrip()) <= indent:
      break
    content.append(line)
  return textwrap.dedent('\n'.join(content)) + '\n'


@pytest.mark.parametrize('source,allowed', [('', True), ('a' * 40, True), ('NEXO', False),
                                          ('a' * 39, False), ('A' * 40, False), ('refs/heads/unreviewed', False),
                                          ('$(touch escaped)', False)])
def test_source_selector_rejects_refs_and_shell_input_before_checkout(tmp_path, source, allowed):
  result = subprocess.run(['bash', '-c', block('Validate source selector')], cwd=tmp_path,
                          env={**os.environ, 'REQUESTED_SOURCE': source}, capture_output=True, text=True)
  assert (result.returncode == 0) is allowed
  assert not (tmp_path / 'escaped').exists()


@pytest.fixture
def repository(tmp_path):
  origin, work = tmp_path / 'origin.git', tmp_path / 'work'
  subprocess.run(['git', 'init', '--bare', str(origin)], capture_output=True, check=True)
  work.mkdir()
  def git(*args):
    return subprocess.run(['git', '-C', str(work), *args], capture_output=True, text=True, check=True).stdout.strip()
  git('init')
  git('config', 'user.name', 'CI source guard test')
  git('config', 'user.email', 'ci@example.invalid')
  git('remote', 'add', 'origin', str(origin))
  def commit(value):
    (work / 'source').write_text(value)
    git('add', 'source')
    git('commit', '-m', value)
    return git('rev-parse', 'HEAD')
  old = commit('approved source')
  git('branch', '-M', 'NEXO')
  head = commit('current source')
  git('push', 'origin', 'NEXO')
  git('checkout', '-b', 'unapproved', old)
  other = commit('not reviewed into NEXO')
  return work, git, old, head, other


@pytest.mark.parametrize('selection,allowed', [('old', True), ('head', True), ('other', False)])
def test_dispatch_uses_real_git_ancestry_and_exact_checked_out_source(repository, selection, allowed):
  work, git, old, head, other = repository
  expected = dict(old=old, head=head, other=other)[selection]
  git('checkout', '--detach', expected)
  output = work / 'outputs'
  result = subprocess.run(['bash', '-c', block('Resolve tested source commit')], cwd=work,
                          env={**os.environ, 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_OUTPUT': str(output)},
                          capture_output=True, text=True)
  assert (result.returncode == 0) is allowed
  assert (output.read_text().strip() == 'commit=' + expected) if allowed else not output.exists()


def test_publish_is_still_gated_by_recovery_and_trusted_nexo_event():
  text = WORKFLOW.read_text()
  publish = text.split('  publish:', 1)[1]
  assert 'needs: recovery' in publish and 'always()' not in publish
  expression = next(line.strip()[4:] for line in publish.splitlines() if line.strip().startswith('if: '))
  for event, ref, allowed in [('push', 'refs/heads/NEXO', True), ('workflow_dispatch', 'refs/heads/NEXO', True),
                              ('pull_request', 'refs/heads/NEXO', False), ('workflow_dispatch', 'refs/heads/unreviewed', False)]:
    condition = expression.replace('github.event_name', repr(event)).replace('github.ref', repr(ref))
    condition = condition.replace('&&', 'and').replace('||', 'or')
    assert eval(condition, {'__builtins__': {}}) is allowed
  script = block('Publish immutable HUD release for the 7000 protected updater')
  subprocess.run(['bash', '-n'], input=script, text=True, check=True, capture_output=True)
  assert '--target "$SOURCE_COMMIT"' in script and 'tag="jetson-hud-${SOURCE_COMMIT}"' in script
  assert 'GITHUB_SHA' not in script


@pytest.mark.parametrize('fault', [None, 'metadata', 'bundle_hash', 'archive_source'])
def test_publish_rejects_mistagged_or_changed_package_before_release_creation(tmp_path, fault):
  expected = 'a' * 40
  content = io.BytesIO()
  source = (('b' * 40 if fault == 'archive_source' else expected) + '\n').encode()
  with tarfile.open(fileobj=content, mode='w:gz') as tar:
    item = tarfile.TarInfo('SOURCE_COMMIT')
    item.size = len(source)
    tar.addfile(item, io.BytesIO(source))
  payload = content.getvalue()
  metadata = {'source_commit': 'b' * 40 if fault == 'metadata' else expected,
              'bundle_sha256': '0' * 64 if fault == 'bundle_hash' else hashlib.sha256(payload).hexdigest()}
  package = tmp_path / 'package'
  package.mkdir()
  with zipfile.ZipFile(package / 'NEXO-Jetson-HUD-repair.zip', 'w') as archive:
    archive.writestr('nexo-hud-repair.json', json.dumps(metadata))
    archive.writestr('nexo-hud-repair.tar.gz', payload)
  script = block('Publish immutable HUD release for the 7000 protected updater')
  python = script.split("python3 - <<'PY'\n", 1)[1].split('\nPY', 1)[0]
  result = subprocess.run([sys.executable, '-c', python], cwd=tmp_path,
                          env={**os.environ, 'SOURCE_COMMIT': expected}, capture_output=True, text=True)
  assert (result.returncode == 0) is (fault is None)
