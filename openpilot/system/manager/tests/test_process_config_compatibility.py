"""Validate real NEXO process constructors without Linux-native imports."""

import ast
from abc import ABC, abstractmethod
import importlib.util
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.jetlink.network import offroad_link_allowed

ROOT = Path(__file__).resolve().parents[1]


def registry(pc=False, tici=True):
  # Execute the production class definitions, including their exact signatures.
  # No permissive replacement constructor can hide an unsupported keyword.
  process_tree = ast.parse((ROOT / 'process.py').read_text(encoding='utf-8'))
  nodes = [n for n in process_tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))]
  future = ast.ImportFrom('__future__', [ast.alias('annotations')], 0)
  ns = dict(
    ABC=ABC,
    abstractmethod=abstractmethod,
    launcher=None,
    nativelauncher=None,
    signal=signal,
    os=os,
    sys=sys,
    importlib=importlib,
    PC=pc,
    TICI=tici,
    platform=SimpleNamespace(system=lambda: 'Linux'),
    car=SimpleNamespace(CarParams=object),
    Params=object,
    offroad_link_allowed=offroad_link_allowed,
  )
  exec(compile(ast.fix_missing_locations(ast.Module([future, *nodes], type_ignores=[])), str(ROOT / 'process.py'), 'exec'), ns)
  tree = ast.parse((ROOT / 'process_config.py').read_text(encoding='utf-8'))
  nodes = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
  exec(compile(ast.fix_missing_locations(ast.Module([future, *nodes], type_ignores=[])), str(ROOT / 'process_config.py'), 'exec'), ns)
  return ns


@pytest.mark.parametrize('pc,tici', [(False, True), (True, False)])
def test_every_process_registration_uses_supported_nexo_constructor(pc, tici):
  ns = registry(pc, tici)
  procs = ns['managed_processes']
  assert len(procs) == len(ns['procs'])
  assert all(name == proc.name for name, proc in procs.items())
  assert {'modeld', 'ui', 'pandad', 'card', 'controlsd', 'signalcolord', 'nexo_jetlink_network'} <= procs.keys()


def test_signal_worker_uses_fresh_interpreter_without_changing_python_process_api():
  ns = registry()
  worker = ns['managed_processes']['signalcolord']
  assert isinstance(worker, ns['NativeProcess'])
  assert worker.cwd == '.'
  assert worker.cmdline == [sys.executable, '-c', 'from openpilot.selfdrive.modeld.signal_color_shadow import main; main()']
  compile(worker.cmdline[2], '<signal observer entrypoint>', 'exec')
  assert isinstance(ns['managed_processes']['modeld'], ns['PythonProcess'])
  assert isinstance(ns['managed_processes']['nexo_jetlink_network'], ns['PythonProcess'])


def test_existing_native_launcher_executes_worker_once_with_expected_context():
  ns = registry()
  worker = ns['managed_processes']['signalcolord']
  launches, chdirs, execs = [], [], []

  class ProcessCapture:
    def __init__(self, name, target, args):
      self.target, self.args = target, args
      launches.append(name)

    def start(self):
      self.target(*self.args)

  env = {}
  ns.update(
    Process=ProcessCapture,
    BASEDIR='/data/openpilot',
    cloudlog=SimpleNamespace(info=lambda *args: None),
    os=SimpleNamespace(path=os.path, environ=env, chdir=chdirs.append, execvp=lambda path, args: execs.append((path, args))),
  )
  worker.start()
  worker.start()
  assert launches == ['signalcolord']
  assert chdirs == [os.path.join('/data/openpilot', '.')]
  assert env == {'MANAGER_DAEMON': 'signalcolord'}
  assert execs == [(sys.executable, worker.cmdline)]
