"""
Copyright (c) 2026-, Zeph Leggett.

This file is part of jetlink and is licensed under the MIT License.
See ../LICENSE.jetlink and ../UPSTREAM.md for provenance and adaptations.
"""
from __future__ import annotations

import os


def background_thread() -> None:
  """Drop the calling thread to SCHED_OTHER 0, retaining its CPU affinity.

  Threads created after config_realtime_process inherit SCHED_FIFO and the core
  pin, so even one that only waits on a condition takes the frame loop's core at
  equal priority on every wake. Call it first in any thread started here. Best
  effort.
  """
  try:
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  except (OSError, AttributeError, ValueError):
    pass
  # Keep the optional display process's affinity; never expand onto model cores.
