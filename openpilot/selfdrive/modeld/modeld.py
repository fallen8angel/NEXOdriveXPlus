#!/usr/bin/env python3
"""Thin opt-in Jetlink wrapper around the unchanged NEXO modeld.

The original implementation is preserved as modeld_local.py.  With no
/data/nexo_jetlink_enabled marker this module is intentionally equivalent to
running modeld_local directly.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import os
import signal
import subprocess
import sys

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.modeld import modeld_local as _base
from openpilot.selfdrive.modeld.jetlink import enabled

_LocalModelState = _base.ModelState
_jetlink_proc = None
_use_jetlink = False


def _child_normal_priority():
  try:
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  except Exception:
    pass
  try:
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
  except Exception:
    pass


def _stop_daemon():
  global _jetlink_proc
  if _jetlink_proc is not None and _jetlink_proc.poll() is None:
    try:
      _jetlink_proc.terminate()
      _jetlink_proc.wait(timeout=2)
    except Exception:
      try:
        _jetlink_proc.kill()
      except Exception:
        pass
  _jetlink_proc = None


def _start_daemon() -> bool:
  global _jetlink_proc
  if not enabled():
    return False
  try:
    if Params().get_bool('NexoJetsonUsb'):
      cloudlog.warning('NEXO Jetlink disabled: NexoJetsonUsb display mode is still ON')
      return False
  except Exception:
    pass
  try:
    _jetlink_proc = subprocess.Popen(
      [sys.executable, '-m', 'openpilot.selfdrive.modeld.jetlink.daemon'],
      preexec_fn=_child_normal_priority,
    )
    atexit.register(_stop_daemon)
    cloudlog.warning('NEXO Jetlink daemon started pid=%d', _jetlink_proc.pid)
    return True
  except Exception:
    cloudlog.exception('NEXO Jetlink daemon failed to start; using local model')
    return False


def _model_state(cam_w: int, cam_h: int, usbgpu: bool):
  local = _LocalModelState(cam_w, cam_h, usbgpu)
  if not _use_jetlink or usbgpu:
    return local
  try:
    from openpilot.selfdrive.modeld.jetlink.model import JoiningModel
    return JoiningModel(local, cam_w, cam_h)
  except Exception:
    cloudlog.exception('NEXO Jetlink wrapper unavailable; retaining local model')
    return local


def main(demo=False):
  global _use_jetlink
  _use_jetlink = _start_daemon()
  _base.ModelState = _model_state
  try:
    return _base.main(demo=demo)
  finally:
    _stop_daemon()


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--demo', action='store_true')
  args = parser.parse_args()
  try:
    main(demo=args.demo)
  except KeyboardInterrupt:
    cloudlog.warning('got SIGINT')
