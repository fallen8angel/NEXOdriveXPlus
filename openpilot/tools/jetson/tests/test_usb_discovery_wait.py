"""Exercise the actual embedded discovery retry loop without device I/O."""
import ast
from pathlib import Path
import re
from types import SimpleNamespace


def retry_loop(results):
  source = (Path(__file__).parents[1] / 'Connect-Jetson.ps1').read_text(encoding='utf-8-sig')
  program = re.search(r"(?ms)^\$discovery = @'\n(.*?)^'@", source).group(1)
  tree = ast.parse(program)
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'wait_for_peer')
  now, calls = [0], []

  def discover():
    calls.append(now[0])
    return results[min(len(calls) - 1, len(results) - 1)]

  def sleep(seconds):
    now[0] += seconds

  namespace = {'discover': discover, 'time': SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)}
  exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual USB discovery wait>', 'exec'), namespace)
  return namespace['wait_for_peer'], calls


EMPTY = {'peers': [], 'errors': []}
READY = {'peers': [{'address': 'fe80::1234', 'ssh': True}], 'errors': []}


def test_default_keeps_single_discovery():
  wait, calls = retry_loop([EMPTY])
  assert wait(0) == EMPTY
  assert calls == [0]


def test_wait_finds_peer_after_boot_without_reauthentication():
  wait, calls = retry_loop([EMPTY, EMPTY, READY])
  assert wait(180) == READY
  assert calls == [0, 2, 4]


def test_no_peer_wait_is_bounded():
  wait, calls = retry_loop([EMPTY])
  assert wait(5) == EMPTY
  assert calls == [0, 2, 4, 5]


def test_wait_preserves_ambiguous_result_for_caller_rejection():
  ambiguous = {'peers': READY['peers'] + [{'address': 'fe80::5678', 'ssh': True}], 'errors': []}
  wait, calls = retry_loop([ambiguous])
  assert wait(180) == ambiguous
  assert calls == [0]
